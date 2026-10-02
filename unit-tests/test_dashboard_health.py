"""The dashboard's consistency checks: quota counters against servers, Deployments against rows."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Optional

from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.orchestrator.dashboard_health import GRACE_MS, find_orphans, quota_drift
from idegym.orchestrator.database.database import RuleUsage

NOW = 1_800_000_000_000
LONG_AGO = NOW - 10 * GRACE_MS


def _rule(rule_id: int = 1, pods: int = 3, cpu: float = 3.0, ram: float = 6.0) -> SimpleNamespace:
    return SimpleNamespace(id=rule_id, current_pods=pods, used_cpu=cpu, used_ram=ram)


def _deployment(name: str, created_ms: int = LONG_AGO, deleting: bool = False) -> SimpleNamespace:
    created = datetime.fromtimestamp(created_ms / 1000, tz=UTC)
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, creation_timestamp=created, deletion_timestamp=created if deleting else None
        )
    )


def _server(
    name: str,
    availability: AvailabilityStatus = AvailabilityStatus.ALIVE,
    namespace: str = "idegym",
    created_at: int = LONG_AGO,
    last_heartbeat_time: Optional[int] = LONG_AGO,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=hash(name) % 1000,
        generated_name=name,
        availability=availability,
        namespace=namespace,
        created_at=created_at,
        last_heartbeat_time=last_heartbeat_time,
    )


def _orphans(deployments: dict[str, list[Any]], servers: list[Any]) -> list[tuple[str, str]]:
    by_name = {server.generated_name: server for server in servers}
    live = [server for server in servers if server.availability in {"ALIVE", "REUSED", "FINISHED"}]
    return [(orphan.name, orphan.problem) for orphan in find_orphans(deployments, by_name, live, now_ms=NOW)]


def test_matching_counters_are_consistent():
    (drift,) = quota_drift([_rule()], {1: RuleUsage(3, 3.0, 6.0)})

    assert drift.consistent


def test_float_rounding_is_not_drift():
    (drift,) = quota_drift([_rule(cpu=0.3)], {1: RuleUsage(3, 0.1 + 0.2, 6.0)})

    assert drift.consistent


def test_a_leaked_release_shows_as_positive_drift():
    (drift,) = quota_drift([_rule(pods=4, cpu=5.0, ram=10.0)], {1: RuleUsage(3, 3.0, 6.0)})

    assert not drift.consistent
    assert (drift.pods, drift.cpu, drift.ram) == (1, 2.0, 4.0)


def test_a_rule_nobody_falls_under_should_be_empty():
    (drift,) = quota_drift([_rule(rule_id=9, pods=1, cpu=1.0, ram=2.0)], {})

    assert drift.actual == RuleUsage(0, 0.0, 0.0)
    assert drift.pods == 1


def test_a_deployment_without_any_row_is_an_orphan():
    assert _orphans({"idegym": [_deployment("ghost-1")]}, []) == [("ghost-1", "No server row owns this Deployment")]


def test_a_deployment_of_an_ended_server_is_an_orphan():
    server = _server("srv-1", availability=AvailabilityStatus.STOPPED)

    ((name, problem),) = _orphans({"idegym": [_deployment("srv-1")]}, [server])

    assert name == "srv-1"
    assert "STOPPED" in problem


def test_a_server_that_just_stopped_gets_a_grace_period():
    server = _server("srv-1", availability=AvailabilityStatus.STOPPED, last_heartbeat_time=NOW - 1000)

    assert _orphans({"idegym": [_deployment("srv-1")]}, [server]) == []


def test_new_and_deleting_deployments_are_skipped():
    deployments = {"idegym": [_deployment("new", created_ms=NOW - 1000), _deployment("leaving", deleting=True)]}

    assert _orphans(deployments, []) == []


def test_a_live_server_without_a_deployment_is_reported():
    ((name, problem),) = _orphans({"idegym": []}, [_server("srv-2")])

    assert name == "srv-2"
    assert "no Deployment" in problem


def test_a_server_that_is_still_starting_is_not_missing_its_deployment():
    assert _orphans({"idegym": []}, [_server("srv-2", created_at=NOW - 1000)]) == []


def test_a_namespace_that_could_not_be_listed_reports_nothing_missing():
    assert _orphans({}, [_server("srv-3", namespace="restricted")]) == []


def test_healthy_servers_produce_no_orphans():
    servers = [_server("srv-1"), _server("srv-2", availability=AvailabilityStatus.FINISHED)]
    deployments = {"idegym": [_deployment("srv-1"), _deployment("srv-2")]}

    assert _orphans(deployments, servers) == []
