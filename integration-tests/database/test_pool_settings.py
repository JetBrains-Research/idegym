"""Integration tests for the connection-pool hardening against a real PostgreSQL instance.

Covers the atomic quota reservation under concurrency, the conditional ``LOCK TABLE clients`` in
client registration, and the per-connection PostgreSQL settings the engine sends through asyncpg.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from idegym.api.config import SQLAlchemyConfig
from idegym.orchestrator.database import helpers
from idegym.orchestrator.database.database import (
    check_resources_and_save_server,
    create_client,
    create_db_engine,
    create_resource_limit_rule,
)
from idegym.orchestrator.database.models import IdeGYMServer, ResourceLimitRule
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

_NAMESPACE = "idegym"


async def test_concurrent_reservations_admit_exactly_pods_limit(db: AsyncSession, db_url: str):
    """200 concurrent reservations against pods_limit=100 create exactly 100 servers and count 100 pods."""
    rule = await create_resource_limit_rule(db, ".*", pods_limit=100, cpu_limit=1e6, ram_limit=1e6, priority=-1)
    client = await create_client(db, "burst-client")

    engine = create_async_engine(db_url, pool_size=10, max_overflow=20, pool_timeout=60)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)

    async def reserve(index: int):
        async with factory() as session:
            return await check_resources_and_save_server(
                session, client.id, client.name, f"burst-{index}", _NAMESPACE, cpu_request=1.0, ram_request=1.0
            )

    try:
        results = await asyncio.gather(*(reserve(index) for index in range(200)))
    finally:
        await engine.dispose()

    admitted = [server for server in results if server is not None]
    assert len(admitted) == 100
    assert len({server.generated_name for server in admitted}) == 100

    assert (await db.execute(select(func.count()).select_from(IdeGYMServer))).scalar_one() == 100
    usage = (
        await db.execute(
            select(ResourceLimitRule.used_cpu, ResourceLimitRule.used_ram, ResourceLimitRule.current_pods).where(
                ResourceLimitRule.id == rule.id
            )
        )
    ).one()
    assert tuple(usage) == (100.0, 100.0, 100)


async def test_register_client_locks_clients_table_only_when_nodes_are_requested(db: AsyncSession, mocker):
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    @asynccontextmanager
    async def test_session():
        yield db

    sync_engine = db.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", capture)
    mocker.patch.object(helpers, "get_db_session", test_session)
    try:
        client, spin_up = await helpers.safely_register_new_client_in_db(
            name="kaiser-worker", nodes_count=0, namespace=_NAMESPACE
        )
        assert client.id is not None
        assert spin_up is False
        assert not any("LOCK TABLE" in statement for statement in statements)

        statements.clear()
        client, spin_up = await helpers.safely_register_new_client_in_db(
            name="node-holder", nodes_count=2, namespace=_NAMESPACE
        )
        assert spin_up is True
        assert any("LOCK TABLE clients IN EXCLUSIVE MODE" in statement for statement in statements)

        # A second holder of the same name with enough nodes: the lock is taken and no spin-up is needed.
        _, spin_up = await helpers.safely_register_new_client_in_db(
            name="node-holder", nodes_count=2, namespace=_NAMESPACE
        )
        assert spin_up is False
    finally:
        event.remove(sync_engine, "before_cursor_execute", capture)


async def test_pooled_connection_carries_configured_server_settings(db_url: str):
    config = SQLAlchemyConfig(
        pool_size=1,
        max_overflow=0,
        lock_timeout_ms=1234,
        statement_timeout_ms=4321,
        idle_in_transaction_timeout_ms=5678,
        application_name="idegym-settings-test",
    )
    engine = create_db_engine(db_url, config)

    async def read_settings() -> dict[str, str]:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT name, setting FROM pg_settings WHERE name IN "
                    "('lock_timeout', 'statement_timeout', 'idle_in_transaction_session_timeout', 'application_name')"
                )
            )
            return {name: setting for name, setting in rows}

    try:
        first = await read_settings()
        # The single pooled connection is checked out again; the settings are per connection, not per checkout.
        second = await read_settings()
    finally:
        await engine.dispose()

    for settings in (first, second):
        assert settings["lock_timeout"] == "1234"
        assert settings["statement_timeout"] == "4321"
        assert settings["idle_in_transaction_session_timeout"] == "5678"
        assert settings["application_name"] == "idegym-settings-test"


async def test_lock_timeout_fails_a_blocked_statement_instead_of_pinning_the_connection(db: AsyncSession, db_url: str):
    """A statement waiting on a row held by another session fails after lock_timeout."""
    rule = await create_resource_limit_rule(db, ".*", pods_limit=10, cpu_limit=10.0, ram_limit=10.0, priority=-1)
    engine = create_db_engine(db_url, SQLAlchemyConfig(pool_size=2, max_overflow=0, lock_timeout_ms=200))
    try:
        async with engine.connect() as holder, engine.connect() as waiter:
            await holder.execute(select(ResourceLimitRule).where(ResourceLimitRule.id == rule.id).with_for_update())
            with pytest.raises(Exception, match="lock timeout"):
                await waiter.execute(select(ResourceLimitRule).where(ResourceLimitRule.id == rule.id).with_for_update())
            await holder.rollback()
    finally:
        await engine.dispose()
