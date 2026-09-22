"""Integration tests for the watcher's reconciliation passes against a real PostgreSQL instance.

``finalize_failed_deletion`` and ``reconcile_resource_usage`` run unmodified; ``reconcile_pods_with_db``
has its Kubernetes helpers mocked in the ``idegym.watcher.reconcile`` namespace.
"""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.backend.utils.kubernetes_client import SANDBOX_LABELS
from idegym.orchestrator.database.database import (
    check_resources_and_save_server,
    create_client,
    create_resource_limit_rule,
    finalize_failed_deletion,
    update_idegym_server_heartbeat,
)
from idegym.orchestrator.database.models import Client, IdeGYMServer, ResourceLimitRule
from idegym.watcher.reconcile import reconcile_pods_with_db, reconcile_resource_usage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_NAMESPACE = "idegym"
GRACE = timedelta(minutes=2)


async def _usage(db: AsyncSession, rule_id: int) -> tuple[float, float, int]:
    """Read the rule's counters straight from the table, bypassing objects cached in the session."""
    row = (
        await db.execute(
            select(ResourceLimitRule.used_cpu, ResourceLimitRule.used_ram, ResourceLimitRule.current_pods).where(
                ResourceLimitRule.id == rule_id
            )
        )
    ).one()
    return row.used_cpu, row.used_ram, row.current_pods


async def _status(db: AsyncSession, server_id: int) -> str:
    return (await db.execute(select(IdeGYMServer.availability).where(IdeGYMServer.id == server_id))).scalar_one()


async def _admit(db: AsyncSession, client: Client, name: str, cpu=1.0, ram=2.0) -> IdeGYMServer:
    server = await check_resources_and_save_server(
        db, client.id, client.name, name, _NAMESPACE, cpu_request=cpu, ram_request=ram
    )
    assert server is not None
    return server


async def _raw_server(db: AsyncSession, client: Client, availability: str, cpu=1.0, ram=2.0) -> IdeGYMServer:
    """Insert a server row directly, without taking quota, in the given status."""
    server = IdeGYMServer(
        client_id=client.id,
        client_name=client.name,
        server_name="raw",
        generated_name=f"raw-{uuid4().hex[:8]}",
        namespace=_NAMESPACE,
        cpu=cpu,
        ram=ram,
        availability=availability,
        last_heartbeat_time=int(time.time() * 1000),
    )
    db.add(server)
    await db.commit()
    return server


def _pod(name: str, *, age=timedelta(minutes=10)):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            labels=dict(SANDBOX_LABELS),
            deletion_timestamp=None,
            creation_timestamp=datetime.now(timezone.utc) - age,
        )
    )


# ---------------------------------------------------------------------------
# finalize_failed_deletion
# ---------------------------------------------------------------------------


async def test_quota_is_released_once_on_entering_deletion_failed_and_not_again_on_finalize(db: AsyncSession):
    rule = await create_resource_limit_rule(db, ".*", pods_limit=10, cpu_limit=20.0, ram_limit=40.0, priority=-1)
    client = await create_client(db, "finalize-client")
    kept = await _admit(db, client, "kept", cpu=4.0, ram=8.0)
    doomed = await _admit(db, client, "doomed", cpu=4.0, ram=8.0)
    assert await _usage(db, rule.id) == (8.0, 16.0, 2)

    await update_idegym_server_heartbeat(db, doomed.id, AvailabilityStatus.DELETION_FAILED)
    assert await _usage(db, rule.id) == (4.0, 8.0, 1)

    finalized = await finalize_failed_deletion(db, doomed.id)
    assert finalized is not None
    assert await _status(db, doomed.id) == AvailabilityStatus.KILLED
    # The kept server's share is untouched: finalize does not subtract a second time.
    assert await _usage(db, rule.id) == (4.0, 8.0, 1)

    # KILLED is terminal: neither finalize nor a heartbeat changes the row or the quota again.
    assert await finalize_failed_deletion(db, doomed.id) is None
    await update_idegym_server_heartbeat(db, doomed.id, AvailabilityStatus.STOPPED)
    assert await _status(db, doomed.id) == AvailabilityStatus.KILLED
    assert await _usage(db, rule.id) == (4.0, 8.0, 1)
    assert await _status(db, kept.id) == AvailabilityStatus.ALIVE


async def test_finalize_ignores_rows_that_are_not_deletion_failed(db: AsyncSession):
    client = await create_client(db, "finalize-noop")
    alive = await _raw_server(db, client, AvailabilityStatus.ALIVE)
    stopped = await _raw_server(db, client, AvailabilityStatus.STOPPED)

    assert await finalize_failed_deletion(db, alive.id) is None
    assert await finalize_failed_deletion(db, stopped.id) is None
    assert await finalize_failed_deletion(db, 987654321) is None
    assert await _status(db, alive.id) == AvailabilityStatus.ALIVE
    assert await _status(db, stopped.id) == AvailabilityStatus.STOPPED


# ---------------------------------------------------------------------------
# reconcile_resource_usage
# ---------------------------------------------------------------------------


async def test_recount_corrects_injected_drift_and_leaves_correct_rules_untouched(db: AsyncSession):
    catch_all = await create_resource_limit_rule(db, ".*", pods_limit=50, cpu_limit=100.0, ram_limit=100.0, priority=-1)
    team = await create_resource_limit_rule(db, "^team-.*", pods_limit=10, cpu_limit=20.0, ram_limit=40.0, priority=1)
    team_client = await create_client(db, "team-alpha")
    other_client = await create_client(db, "solo")

    await _admit(db, team_client, "t1", cpu=1.0, ram=2.0)
    await _admit(db, team_client, "t2", cpu=1.5, ram=2.5)
    finished = await _admit(db, other_client, "o1", cpu=3.0, ram=4.0)
    await update_idegym_server_heartbeat(db, finished.id, AvailabilityStatus.FINISHED)
    # Terminal rows hold no quota and must not be counted.
    await _raw_server(db, team_client, AvailabilityStatus.STOPPED, cpu=7.0, ram=7.0)
    await _raw_server(db, other_client, AvailabilityStatus.DELETION_FAILED, cpu=9.0, ram=9.0)
    assert await _usage(db, team.id) == (2.5, 4.5, 2)
    assert await _usage(db, catch_all.id) == (3.0, 4.0, 1)

    await db.execute(
        update(ResourceLimitRule)
        .where(ResourceLimitRule.id == catch_all.id)
        .values(used_cpu=432.0, used_ram=1080.0, current_pods=216)
    )
    await db.commit()

    stats = await reconcile_resource_usage(db)

    assert stats is not None
    assert [drift.rule_id for drift in stats.corrected] == [catch_all.id]
    assert stats.corrected[0].stored == (432.0, 1080.0, 216)
    assert stats.corrected[0].actual == (3.0, 4.0, 1)
    assert stats.live_servers == 3
    assert await _usage(db, catch_all.id) == (3.0, 4.0, 1)
    assert await _usage(db, team.id) == (2.5, 4.5, 2)

    again = await reconcile_resource_usage(db)
    assert again is not None
    assert again.corrected == []


async def test_recount_zeroes_a_rule_with_no_live_servers(db: AsyncSession):
    rule = await create_resource_limit_rule(
        db,
        ".*",
        pods_limit=50,
        cpu_limit=100.0,
        ram_limit=100.0,
        priority=-1,
        used_cpu=5.0,
        used_ram=6.0,
        current_pods=7,
    )
    client = await create_client(db, "ghosts")
    await _raw_server(db, client, AvailabilityStatus.DELETION_FAILED)
    await _raw_server(db, client, AvailabilityStatus.KILLED)

    stats = await reconcile_resource_usage(db)

    assert stats is not None
    assert len(stats.corrected) == 1
    assert await _usage(db, rule.id) == (0.0, 0.0, 0)


async def test_recount_assigns_each_server_to_its_highest_priority_rule(db: AsyncSession):
    low = await create_resource_limit_rule(db, "^team-.*", pods_limit=10, cpu_limit=20.0, ram_limit=40.0, priority=0)
    high = await create_resource_limit_rule(db, "^team-a.*", pods_limit=10, cpu_limit=20.0, ram_limit=40.0, priority=5)
    client = await create_client(db, "team-alpha")
    await _admit(db, client, "s", cpu=1.0, ram=1.0)
    assert await _usage(db, high.id) == (1.0, 1.0, 1)
    assert await _usage(db, low.id) == (0.0, 0.0, 0)

    await db.execute(update(ResourceLimitRule).where(ResourceLimitRule.id == low.id).values(current_pods=3))
    await db.commit()

    stats = await reconcile_resource_usage(db)

    assert stats is not None
    assert [drift.rule_id for drift in stats.corrected] == [low.id]
    assert await _usage(db, high.id) == (1.0, 1.0, 1)
    assert await _usage(db, low.id) == (0.0, 0.0, 0)


# ---------------------------------------------------------------------------
# reconcile_pods_with_db on PostgreSQL
# ---------------------------------------------------------------------------


async def test_reconcile_pods_reaps_orphans_and_finalizes_failed_rows(db: AsyncSession, mocker):
    rule = await create_resource_limit_rule(db, ".*", pods_limit=10, cpu_limit=20.0, ram_limit=40.0, priority=-1)
    client = await create_client(db, "reap-client")
    alive = await _admit(db, client, "alive")
    failed_with_pod = await _admit(db, client, "failed-pod")
    failed_no_pod = await _admit(db, client, "failed-nopod")
    stopped = await _admit(db, client, "stopped")
    for server, status in (
        (failed_with_pod, AvailabilityStatus.DELETION_FAILED),
        (failed_no_pod, AvailabilityStatus.DELETION_FAILED),
        (stopped, AvailabilityStatus.STOPPED),
    ):
        await update_idegym_server_heartbeat(db, server.id, status)
    assert await _usage(db, rule.id) == (1.0, 2.0, 1)

    pods = [
        _pod(alive.generated_name),
        _pod(failed_with_pod.generated_name),
        _pod(stopped.generated_name),
        _pod("ghost-42"),
        _pod("fresh-7", age=timedelta(seconds=10)),
    ]
    mocker.patch("idegym.watcher.reconcile.list_pods", new=mocker.AsyncMock(return_value=pods))
    clean_up = mocker.patch("idegym.watcher.reconcile.clean_up_server", new=mocker.AsyncMock())

    stats = await reconcile_pods_with_db(db, _NAMESPACE, GRACE)

    assert stats is not None
    deleted = {call.kwargs["name"] for call in clean_up.await_args_list}
    assert deleted == {failed_with_pod.generated_name, stopped.generated_name, "ghost-42"}
    assert (stats.pods_scanned, stats.orphans_deleted, stats.rows_finalized, stats.skipped_young) == (5, 3, 2, 1)
    assert await _status(db, alive.id) == AvailabilityStatus.ALIVE
    assert await _status(db, failed_with_pod.id) == AvailabilityStatus.KILLED
    assert await _status(db, failed_no_pod.id) == AvailabilityStatus.KILLED
    assert await _status(db, stopped.id) == AvailabilityStatus.STOPPED
    # Quota was released when the rows entered their terminal statuses; finalizing adds nothing.
    assert await _usage(db, rule.id) == (1.0, 2.0, 1)
