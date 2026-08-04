import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from idegym.api.exceptions import MigrationError
from idegym.utils.logging import get_logger
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

logger = get_logger(__name__)

MIGRATION_LOCK_ID = 42239

# Alembic's own name for "no revision applied"; a valid downgrade target, never a revision id.
BASE_REVISION = "base"

# Aliases Alembic accepts for the newest revision. Rollback must never use them: it targets
# the exact revision the older release declared.
HEAD_ALIASES = frozenset({"head", "heads"})


class MigrationDirection(StrEnum):
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"
    NOOP = "noop"


@dataclass(frozen=True)
class MigrationPlan:
    """What a migration would do, resolved before anything touches the database.

    ``revisions`` lists the revisions in execution order — applied for an upgrade,
    reverted for a downgrade — so an operator can see the blast radius of a downgrade
    before approving it.
    """

    direction: MigrationDirection
    current: Optional[str]
    target: str
    revisions: tuple[str, ...]

    def describe(self) -> str:
        current = self.current or BASE_REVISION
        if self.direction is MigrationDirection.NOOP:
            return f"database is already at revision {current}"
        return f"{self.direction} {current} -> {self.target} via {', '.join(self.revisions)}"


class MigrationManager:
    """
    Manages Alembic migrations using a PostgreSQL advisory lock to ensure
    only one orchestrator replica runs migrations when multiple start simultaneously.
    """

    def __init__(self, engine: AsyncEngine, db_url: str, timeout: float = 300.0):
        self.engine = engine
        self.db_url = db_url
        self.timeout = timeout
        self.alembic_ini_path = str(Path(__file__).parent / "alembic.ini")

    async def clean_database(self) -> None:
        async with self.engine.begin() as conn:
            tables_sql = text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY schemaname;")
            result = await conn.execute(tables_sql)
            tables = [row[0] for row in result]

            for table_name in tables:
                await conn.execute(text(f'DROP TABLE IF EXISTS public."{table_name}" CASCADE;'))
                logger.info(f"Dropped table public.{table_name}")

    async def run_migrations(self) -> bool:
        """
        Run migrations under a PostgreSQL advisory lock.

        Returns True if this process ran migrations, False if another process already held the lock.
        """
        try:
            async with asyncio.TaskGroup() as tg:
                migration_task = tg.create_task(self._run_migrations_with_lock())
            migration_result = migration_task.result()
            return migration_result

        except* TimeoutError as eg:
            logger.error(f"Migration timeout after {self.timeout} seconds")
            raise TimeoutError("Database migration timed out") from eg.exceptions[0]
        except* Exception as eg:
            logger.exception("Error running migrations with structured concurrency")
            raise eg.exceptions[0]

    async def _run_migrations_with_lock(self) -> bool:
        async with self._advisory_lock() as lock_connection:
            if lock_connection is None:
                logger.info("Another process is already running migrations, skipping")
                return False

            logger.info("Acquired migration lock, running migrations")
            async with asyncio.timeout(self.timeout):
                await self._run_alembic_migrations()
            logger.info("Database migrations completed successfully")
            return True

    @asynccontextmanager
    async def _advisory_lock(self) -> AsyncIterator[Optional[AsyncConnection]]:
        """Try to take the migration lock, yielding the holding connection, or None.

        The connection is handed to the caller rather than kept private because the pool
        can be a single connection — the migration CLI runs with one — so anything the
        lock holder needs to read has to reuse it instead of waiting for a second.

        The lock only serialises IdeGYM processes against each other. It does not stop an
        orchestrator or watcher that is already running from writing while the schema
        changes, so a downgrade still requires those writers to be stopped first.
        """
        async with self.engine.begin() as conn:
            result = await conn.execute(text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID})
            lock_acquired = bool(result.scalar())
            try:
                yield conn if lock_acquired else None
            finally:
                if lock_acquired:
                    await conn.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID})
                    logger.info("Released migration lock")

    async def _run_alembic_migrations(self):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._execute_alembic_upgrade())

        logger.info("All migration tasks completed successfully")

    async def _execute_alembic_upgrade(self):
        await asyncio.to_thread(self._run_alembic_upgrade)

    def _run_alembic_upgrade(self):
        try:
            command.upgrade(self._alembic_config(), "heads")
            logger.info("Alembic upgrade command completed successfully")
        except Exception as e:
            logger.exception("Alembic upgrade failed")
            raise RuntimeError(f"Database migration failed: {e}") from e

    async def migrate_to(self, target: str, *, allow_downgrade: bool = False) -> MigrationPlan:
        """Move the schema to exactly ``target``, in whichever direction that requires.

        This is the deployment-rollback counterpart of :meth:`run_migrations`, which only
        ever moves forward. Unlike startup migrations, a lock held by someone else is an
        error rather than a reason to skip: the caller asked for a specific revision and
        must not be told it succeeded when nothing ran.

        ``allow_downgrade`` has to be set explicitly for a backwards move, so a mistyped
        target cannot silently drop columns.
        """
        plan = self.plan_migration(current=await self.get_current_revision(), target=target)
        if plan.direction is MigrationDirection.DOWNGRADE and not allow_downgrade:
            raise MigrationError(
                f"Refusing to {plan.describe()} without an explicit downgrade approval; "
                "a downgrade may discard data written by the reverted revisions"
            )

        if plan.direction is MigrationDirection.NOOP:
            logger.info("No migration needed", revision=plan.target)
            return plan

        async with self._advisory_lock() as lock_connection:
            if lock_connection is None:
                raise MigrationError(
                    "Another process holds the migration lock; a migration or rollback is already in progress"
                )

            # The revision may have moved between planning and taking the lock — a starting
            # orchestrator replica migrates on its own. Re-plan rather than apply a stale plan.
            locked_current = await self._read_current_revision(lock_connection)
            locked_plan = self.plan_migration(current=locked_current, target=target)
            if locked_plan != plan:
                raise MigrationError(
                    f"Database revision changed while acquiring the migration lock "
                    f"({plan.current} -> {locked_plan.current}); stop all writers and retry"
                )

            logger.info("Acquired migration lock", plan=plan.describe())
            async with asyncio.timeout(self.timeout):
                await asyncio.to_thread(self._run_alembic, plan)

        logger.info("Migration completed", plan=plan.describe())
        return plan

    def _run_alembic(self, plan: MigrationPlan) -> None:
        try:
            if plan.direction is MigrationDirection.DOWNGRADE:
                command.downgrade(self._alembic_config(), plan.target)
            else:
                command.upgrade(self._alembic_config(), plan.target)
        except Exception as e:
            logger.exception("Alembic migration failed", plan=plan.describe())
            raise MigrationError(f"Failed to {plan.describe()}: {e}") from e

    def plan_migration(self, current: Optional[str], target: str) -> MigrationPlan:
        """Resolve ``current -> target`` into a direction and the revisions it traverses.

        Raises rather than guesses whenever the move is not well defined: an unknown
        target, a database sitting on a revision this image does not contain (the
        signature of rolling back with the wrong image), or a revision being reverted
        that ships no down migration.
        """
        chain = self.revision_chain()
        resolved = self._resolve_target(target, chain)
        current_position = self._position(current, chain, "Database")
        target_position = self._position(resolved, chain, "Target")

        if target_position == current_position:
            return MigrationPlan(MigrationDirection.NOOP, current, resolved, ())

        if target_position > current_position:
            applied = tuple(chain[current_position + 1 : target_position + 1])
            return MigrationPlan(MigrationDirection.UPGRADE, current, resolved, applied)

        reverted = tuple(reversed(chain[target_position + 1 : current_position + 1]))
        self._require_reversible(reverted)
        return MigrationPlan(MigrationDirection.DOWNGRADE, current, resolved, reverted)

    def revision_chain(self) -> list[str]:
        """Revision ids ordered base -> head.

        A rollback targets one exact revision, which only means something while the
        history is a single unbranched line, so a merge point is rejected rather than
        traversed in an arbitrary order.
        """
        self.head_revision()
        chain: list[str] = []
        for script in self._script_directory().walk_revisions():
            down_revision = script.down_revision
            if isinstance(down_revision, tuple) and len(down_revision) > 1:
                raise MigrationError(
                    f"Revision {script.revision} merges {len(down_revision)} branches; "
                    "exact-revision migration needs a linear history"
                )
            chain.append(script.revision)
        chain.reverse()
        return chain

    def head_revision(self) -> str:
        """Return the single Alembic head revision this image can migrate to."""
        heads = self._script_directory().get_heads()
        if len(heads) != 1:
            raise MigrationError(f"Expected exactly one migration head, found {len(heads)}: {sorted(heads)}")
        return heads[0]

    def verify_declared_revision(self, declared: Optional[str]) -> None:
        """Fail fast when the release declares a schema revision this image cannot produce.

        The chart's ``database.schemaRevision`` is what a rollback downgrades to, so a
        value that does not match the image's head would send a later rollback to the
        wrong revision. Refusing to start turns that into a failed rollout instead.
        """
        if not declared:
            return

        head = self.head_revision()
        if declared != head:
            raise MigrationError(
                f"Release declares database schema revision {declared!r} but this image's migrations "
                f"end at {head!r}; align database.schemaRevision in the chart with the deployed image"
            )

    async def get_current_revision(self) -> Optional[str]:
        """Return the revision recorded in ``alembic_version``, or None if none is."""
        async with self.engine.connect() as conn:
            return await self._read_current_revision(conn)

    async def _read_current_revision(self, conn: AsyncConnection) -> Optional[str]:
        heads = await conn.run_sync(self._current_heads)
        if len(heads) > 1:
            raise MigrationError(f"Database records {len(heads)} applied heads: {sorted(heads)}")
        return heads[0] if heads else None

    def _resolve_target(self, target: str, chain: list[str]) -> str:
        if target in HEAD_ALIASES:
            return chain[-1]
        return target

    def _position(self, revision: Optional[str], chain: list[str], role: str) -> int:
        """Index of ``revision`` in the chain, with ``base``/unmigrated at -1."""
        if revision is None or revision == BASE_REVISION:
            return -1
        try:
            return chain.index(revision)
        except ValueError:
            raise MigrationError(
                f"{role} revision {revision!r} is not one of the migrations in this image "
                f"({', '.join(chain)}); use the image that introduced it"
            ) from None

    def _require_reversible(self, revisions: tuple[str, ...]) -> None:
        """Reject a downgrade whose revisions were never made reversible.

        Migrations are SQL-first: every revision ships ``<rev>_down.sql`` beside its
        module, an invariant ``unit-tests/test_migrations.py`` enforces. A missing file
        therefore means the revision has no downgrade, not that it uses another mechanism.
        """
        versions = Path(self._script_directory().versions)
        missing = [revision for revision in revisions if not (versions / f"{revision}_down.sql").is_file()]
        if missing:
            raise MigrationError(
                f"Revisions {', '.join(missing)} ship no down migration and cannot be reverted; "
                "restore a database backup instead"
            )

    def _alembic_config(self) -> Config:
        alembic_cfg = Config(self.alembic_ini_path)
        alembic_cfg.set_main_option("sqlalchemy.url", self.db_url)
        return alembic_cfg

    def _script_directory(self) -> ScriptDirectory:
        try:
            return ScriptDirectory.from_config(self._alembic_config())
        except Exception as e:
            raise MigrationError(f"Failed to read the migration scripts: {e}") from e

    @staticmethod
    def _current_heads(connection: Connection) -> tuple[str, ...]:
        return MigrationContext.configure(connection).get_current_heads()
