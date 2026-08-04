"""Command-line access to the orchestrator's database schema.

The orchestrator only ever migrates forward, at startup. Rolling a release back needs the
opposite: moving the schema to one *exact* revision, in a one-shot pod, with every IdeGYM
writer stopped. This module is that entry point, and it reads the same Hydra/``POSTGRES_*``
configuration as the service, so a Job that copies the orchestrator Deployment's
environment needs no extra wiring.

Nothing here is safe to run against a live deployment on its own —
``scripts/rollback.py`` is what stops the writers and sequences the steps. Run this
directly only to inspect the schema, or when following the manual procedure in
`Database Rollback <../../../website/docs/reference/database_rollback.md>`_.

Usage::

    python -m idegym.orchestrator.db_cli schema current          # revision the database is at
    python -m idegym.orchestrator.db_cli schema head             # revision this image can reach
    python -m idegym.orchestrator.db_cli schema verify --expect 003
    python -m idegym.orchestrator.db_cli migrate --target 003
    python -m idegym.orchestrator.db_cli migrate --target 002 --allow-downgrade
    python -m idegym.orchestrator.db_cli migrate --target 002 --dry-run
"""

import asyncio
import sys
from argparse import ArgumentParser, Namespace
from typing import Optional

from idegym.api.config import Config
from idegym.api.exceptions import MigrationError
from idegym.backend.utils.logging import configure_logging, configure_sqlalchemy_logging
from idegym.orchestrator.config import load_config
from idegym.orchestrator.migration_manager import BASE_REVISION, MigrationDirection, MigrationManager
from idegym.utils.logging import get_logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="idegym-db", description="Inspect and migrate the IdeGYM database schema.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    schema = subcommands.add_parser("schema", help="Inspect the schema revision").add_subparsers(
        dest="schema_command", required=True
    )
    schema.add_parser("current", help="Print the revision recorded in the database")
    schema.add_parser("head", help="Print the newest revision this image contains")
    verify = schema.add_parser("verify", help="Exit non-zero unless the database is at an exact revision")
    verify.add_argument("--expect", required=True, metavar="REVISION", help=f"Revision to require, or {BASE_REVISION}")

    migrate = subcommands.add_parser("migrate", help="Migrate the schema to an exact revision")
    migrate.add_argument(
        "--target",
        required=True,
        metavar="REVISION",
        help=f"Exact revision to end at, or {BASE_REVISION}. Relative targets such as -1 are not accepted",
    )
    migrate.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="Approve reverting revisions. Required for any backwards move, which may discard data",
    )
    migrate.add_argument("--dry-run", action="store_true", help="Print the plan without touching the database")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()
    configure_logging(config=config.logging)
    configure_sqlalchemy_logging(config=config.logging)

    try:
        return asyncio.run(_dispatch(args, config))
    except MigrationError as e:
        # The message is the operator-facing half of the failure; the traceback is noise.
        print(f"error: {e}", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, SQLAlchemyError) as e:
        database = config.orchestrator.database
        print(f"error: cannot use the database at {database.host}:{database.port}/{database.db}: {e}", file=sys.stderr)
        return EXIT_ERROR


async def _dispatch(args: Namespace, config: Config) -> int:
    database = config.orchestrator.database
    # A one-shot command needs one connection, and NullPool would reconnect per statement.
    engine = create_async_engine(database.url, pool_size=1, max_overflow=0)
    try:
        manager = MigrationManager(engine=engine, db_url=database.url)
        if args.command == "schema":
            return await _run_schema_command(args, manager)
        return await _run_migrate_command(args, manager)
    finally:
        await engine.dispose()


async def _run_schema_command(args: Namespace, manager: MigrationManager) -> int:
    if args.schema_command == "head":
        print(manager.head_revision())
        return EXIT_OK

    current = await manager.get_current_revision() or BASE_REVISION
    if args.schema_command == "current":
        print(current)
        return EXIT_OK

    print(current)
    if current != args.expect:
        print(f"error: database is at revision {current}, expected {args.expect}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


async def _run_migrate_command(args: Namespace, manager: MigrationManager) -> int:
    if args.dry_run:
        plan = manager.plan_migration(current=await manager.get_current_revision(), target=args.target)
        print(plan.describe())
        if plan.direction is MigrationDirection.DOWNGRADE and not args.allow_downgrade:
            print("(would refuse to run without --allow-downgrade)")
        return EXIT_OK

    plan = await manager.migrate_to(args.target, allow_downgrade=args.allow_downgrade)
    print(plan.describe())
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
