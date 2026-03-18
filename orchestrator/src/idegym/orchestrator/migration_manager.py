import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from idegym.utils.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = get_logger(__name__)


class MigrationManager:
    """
    Manages database migrations using Alembic with structured concurrency.
    Ensures migrations run only once when multiple orchestrators start simultaneously
    by using PostgreSQL advisory locks.
    """

    def __init__(self, engine: AsyncEngine, db_url: str, timeout: float = 300.0):
        self.engine = engine
        self.db_url = db_url
        self.timeout = timeout
        orchestrator_dir = Path(__file__).parent.parent.parent.parent
        self.alembic_ini_path = str(orchestrator_dir / "alembic.ini")

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
        Run database migrations with timeout and health monitoring.

        Returns:
            bool: True if migrations were run, False if another process is running migrations
        """
        try:
            async with asyncio.TaskGroup() as tg:
                migration_task = tg.create_task(self._run_migrations_with_lock())
            migration_result = migration_task.result()
            return migration_result

        except* asyncio.TimeoutError as eg:
            logger.error(f"Migration timeout after {self.timeout} seconds")
            raise asyncio.TimeoutError("Database migration timed out") from eg.exceptions[0]
        except* Exception as eg:
            logger.exception("Error running migrations with structured concurrency")
            raise eg.exceptions[0]

    async def _run_migrations_with_lock(self) -> bool:
        """Run migrations with advisory lock, wrapped for task group usage."""
        async with self.engine.begin() as conn:
            result = await conn.execute(text("SELECT pg_try_advisory_lock(42239)"))
            lock_acquired = result.scalar()

            if not lock_acquired:
                logger.info("Another process is already running migrations, skipping")
                return False

            logger.info("Acquired migration lock, running migrations")

            try:
                async with asyncio.timeout(self.timeout):
                    await self._run_alembic_migrations()
                logger.info("Database migrations completed successfully")
                return True
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(42239)"))
                logger.info("Released migration lock")

    async def _run_alembic_migrations(self):
        """Run Alembic migrations with structured concurrency for parallel operations."""
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._execute_alembic_upgrade())

        logger.info("All migration tasks completed successfully")

    async def _execute_alembic_upgrade(self):
        """Execute the actual Alembic upgrade in a thread."""
        await asyncio.to_thread(self._run_alembic_upgrade)

    def _run_alembic_upgrade(self):
        """Run Alembic upgrade command."""
        try:
            alembic_cfg = Config(self.alembic_ini_path)
            alembic_cfg.set_main_option("sqlalchemy.url", self.db_url)
            command.upgrade(alembic_cfg, "heads")
            logger.info("Alembic upgrade command completed successfully")
        except Exception as e:
            logger.exception(f"Alembic upgrade failed: {e}")
            raise RuntimeError(f"Database migration failed: {e}") from e

    def get_expected_version(self) -> str:
        """Get the expected migration version (latest head revision) from Alembic."""
        try:
            alembic_cfg = Config(self.alembic_ini_path)
            script = ScriptDirectory.from_config(alembic_cfg)
            # Get the head revision(s) - returns a tuple of head revisions
            heads = script.get_heads()
            if not heads:
                raise ValueError("No migration heads found")
            return heads[0] if heads else None
        except Exception as e:
            logger.exception(f"Failed to get expected migration version: {e}")
            raise RuntimeError(f"Failed to get expected migration version: {e}") from e
