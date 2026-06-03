"""PostgreSQL container fixtures, scoped to the database integration tests."""

import os

# testcontainers ships a Ryuk sidecar that reaps tracked containers if its
# heartbeat socket goes quiet for RYUK_RECONNECTION_TIMEOUT (default 10s).
# With pytest-asyncio rebuilding event loops per test and pytest-randomly
# reordering files, that 10s window is regularly exceeded between modules,
# causing the postgres container to vanish mid-session and every subsequent
# test to fail with a connection-refused on the cached mapped port. The
# `pg_container` fixture below already stops the container in its finally
# block, so Ryuk has nothing to clean up. Must be set before any
# `testcontainers` import.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import pytest  # noqa: E402

_PG_IMAGE = "postgres:16"


@pytest.fixture(scope="session")
def pg_container():
    """Start a PostgreSQL Docker container for the test session."""
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(_PG_IMAGE)
    container.start()
    try:
        yield container
    finally:
        try:
            container.stop()
        except Exception:
            # Container may have already been removed - ignore cleanup errors
            pass


@pytest.fixture(scope="session")
def db_url(pg_container) -> str:
    """Build an asyncpg-compatible URL from the running container."""
    sync_url = pg_container.get_connection_url()
    return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def db(db_url: str):
    """
    Function-scoped async database session.

    Creates all ORM tables before each test and truncates them after, so
    every test starts with a clean schema and empty data.

    pool_size=1 / max_overflow=0 keeps each test to a single connection so
    the session never exhausts PostgreSQL's max_connections across the many
    sequential tests in this suite.
    """
    from idegym.orchestrator.database.models import Base
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url, echo=False, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

        async with session_factory() as session:
            yield session

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE TABLE snapshot_jobs, snapshots, snapshot_prepare_requests,"
                    " async_operations, job_statuses, servers, resource_limit_rules, clients"
                    " RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await engine.dispose()
