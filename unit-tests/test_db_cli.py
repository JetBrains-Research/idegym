"""Unit tests for migration planning and the ``idegym-db`` argument surface.

Everything here is decided before a connection is opened, so it needs no database: the
plan, the guards that stop a downgrade from happening by accident, and the CLI's contract.
The round-trip against real PostgreSQL lives in
``integration-tests/database/test_migration_roundtrip.py``.
"""

import pytest
from idegym.api.exceptions import MigrationError
from idegym.orchestrator.db_cli import Command, SchemaCommand, build_parser
from idegym.orchestrator.migration_manager import BASE_REVISION, MigrationDirection, MigrationManager
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture
def manager() -> MigrationManager:
    """A manager over an engine that is never connected — planning is offline."""
    url = "postgresql+asyncpg://idegym:idegym@localhost:5432/idegym"
    return MigrationManager(engine=create_async_engine(url), db_url=url)


def test_revision_chain_is_ordered_base_to_head(manager: MigrationManager):
    chain = manager.revision_chain()
    assert chain[0] == "001"
    assert chain[-1] == manager.head_revision()
    assert chain == sorted(chain), "sequential revision ids should walk in order"


def test_upgrade_plan_lists_the_revisions_it_applies(manager: MigrationManager):
    plan = manager.plan_migration(current=None, target="heads")

    assert plan.direction is MigrationDirection.UPGRADE
    assert plan.target == manager.head_revision()
    assert plan.revisions == tuple(manager.revision_chain())


def test_downgrade_plan_lists_the_revisions_it_reverts_newest_first(manager: MigrationManager):
    plan = manager.plan_migration(current="003", target="001")

    assert plan.direction is MigrationDirection.DOWNGRADE
    assert plan.revisions == ("003", "002")
    assert "downgrade 003 -> 001" in plan.describe()


def test_downgrade_to_base_is_a_valid_target(manager: MigrationManager):
    plan = manager.plan_migration(current="001", target=BASE_REVISION)

    assert plan.direction is MigrationDirection.DOWNGRADE
    assert plan.revisions == ("001",)


def test_same_revision_is_a_noop(manager: MigrationManager):
    plan = manager.plan_migration(current="002", target="002")

    assert plan.direction is MigrationDirection.NOOP
    assert plan.revisions == ()
    assert "already at revision 002" in plan.describe()


def test_unknown_target_is_rejected(manager: MigrationManager):
    with pytest.raises(MigrationError, match="Target revision '404'"):
        manager.plan_migration(current="003", target="404")


def test_database_on_an_unknown_revision_is_rejected(manager: MigrationManager):
    """The signature of rolling back with an image older than the database.

    Alembic cannot traverse a revision it has no script for, so this has to fail with an
    explanation rather than a KeyError from inside Alembic.
    """
    with pytest.raises(MigrationError, match="use the image that introduced it"):
        manager.plan_migration(current="099", target="003")


def test_declared_revision_must_match_the_image_head(manager: MigrationManager):
    manager.verify_declared_revision(None)
    manager.verify_declared_revision(manager.head_revision())

    with pytest.raises(MigrationError, match="align database.schemaRevision"):
        manager.verify_declared_revision("002")


def test_migrate_requires_an_exact_target():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([Command.MIGRATE])

    args = parser.parse_args([Command.MIGRATE, "--target", "002", "--allow-downgrade"])
    assert (args.target, args.allow_downgrade, args.dry_run) == ("002", True, False)


def test_downgrade_approval_is_off_by_default():
    args = build_parser().parse_args([Command.MIGRATE, "--target", "002"])

    assert args.allow_downgrade is False


def test_schema_verify_requires_an_expected_revision():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([Command.SCHEMA, SchemaCommand.VERIFY])

    expect = parser.parse_args([Command.SCHEMA, SchemaCommand.VERIFY, "--expect", "003"]).expect
    assert expect == "003"


@pytest.mark.parametrize("subcommand", list(SchemaCommand))
def test_every_schema_subcommand_is_accepted(subcommand: SchemaCommand):
    """The enums name the CLI surface, so a member with no subparser is a typo, not a feature."""
    extra = ["--expect", "003"] if subcommand is SchemaCommand.VERIFY else []

    args = build_parser().parse_args([Command.SCHEMA, subcommand, *extra])

    assert args.command == Command.SCHEMA
    assert args.schema_command == subcommand


@pytest.mark.parametrize("command", list(Command))
def test_every_top_level_command_is_accepted(command: Command):
    extra = ["--target", "003"] if command is Command.MIGRATE else [SchemaCommand.HEAD]

    assert build_parser().parse_args([command, *extra]).command == command
