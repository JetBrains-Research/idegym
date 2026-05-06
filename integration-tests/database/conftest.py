"""PostgreSQL container fixtures, scoped to the database integration tests."""

import pytest

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
    """
    from idegym.orchestrator.database.models import Base
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE async_operations, job_statuses, servers, resource_limit_rules, clients"
                " RESTART IDENTITY CASCADE"
            )
        )

    await engine.dispose()
