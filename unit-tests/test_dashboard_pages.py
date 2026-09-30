"""The dashboard pages render.

Each handler is called directly with its database and Kubernetes calls mocked. The status code is
asserted every time: ``render_dashboard_error`` turns any exception, a template bug included, into
a rendered error page, so "some HTML came back" proves nothing on its own.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from idegym.api.config import Config, GrafanaConfig
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.orchestrator.router import dashboard
from idegym.orchestrator.templating import format_ts, iso_ts, status_level, usage
from starlette.requests import Request

GRAFANA = GrafanaConfig(
    url="https://grafana.example.com",
    loki_datasource_uid="loki-uid",
    tempo_datasource_uid="tempo-uid",
)


def _request(path: str = "/", grafana: GrafanaConfig = GRAFANA) -> Request:
    config = Config()
    config.orchestrator.dashboard.grafana = grafana
    config.otel.service_name = "idegym"
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 8000),
            "client": ("testclient", 50000),
            "app": SimpleNamespace(state=SimpleNamespace(config=config)),
        }
    )


def _server(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": 7,
        "client_id": uuid4(),
        "client_name": "team-alpha",
        "server_name": "srv",
        "generated_name": "srv-7",
        "namespace": "idegym",
        "availability": AvailabilityStatus.ALIVE,
        "image_tag": "registry.example.com/env:latest",
        "container_runtime": "gvisor",
        "server_kind": "idegym",
        "cpu": 2.0,
        "ram": 4.0,
        "created_at": 1_700_000_000_000,
        "last_heartbeat_time": 1_700_000_060_000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _client(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "name": "team-alpha",
        "namespace": "idegym",
        "availability": AvailabilityStatus.ALIVE,
        "nodes_count": 2,
        "created_at": 1_700_000_000_000,
        "last_heartbeat_time": 1_700_000_060_000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rule(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": 1,
        "client_name_regex": ".*",
        "priority": 0,
        "pods_limit": 10,
        "current_pods": 9,
        "cpu_limit": 16.0,
        "used_cpu": 4.0,
        "ram_limit": 32.0,
        "used_ram": 31.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def database(mocker):
    @asynccontextmanager
    async def session():
        yield object()

    mocker.patch.object(dashboard, "get_db_session", session)
    mocker.patch.object(dashboard, "get_running_idegym_servers", mocker.AsyncMock(return_value=[_server()]))
    mocker.patch.object(dashboard, "get_alive_clients", mocker.AsyncMock(return_value=[_client()]))
    mocker.patch.object(dashboard, "_all_rules", mocker.AsyncMock(return_value=[_rule()]))


def _html(response) -> str:
    assert response.status_code == 200, response.body.decode()
    return response.body.decode()


async def test_overview_shows_tiles_meters_and_grafana_shortcuts(database) -> None:
    html = _html(await dashboard.root_page(_request()))

    assert "Alive servers" in html
    assert "meter-critical" in html  # 31 of 32 GB is past the 90% band
    assert "Namespace logs" in html
    assert "Orchestrator traces" in html


async def test_servers_page_links_each_server_to_grafana(database) -> None:
    html = _html(await dashboard.dashboard_servers(_request("/dashboard/servers")))

    assert "srv-7" in html
    assert "grafana.example.com/explore" in html
    assert 'aria-current="page"' in html


async def test_pages_show_no_grafana_column_without_grafana(database) -> None:
    html = _html(await dashboard.dashboard_servers(_request("/dashboard/servers", grafana=GrafanaConfig())))

    assert "grafana.example.com" not in html
    assert "<th>Grafana</th>" not in html


async def test_clients_page_renders(database) -> None:
    html = _html(await dashboard.dashboard_clients(_request("/dashboard/clients")))

    assert "team-alpha" in html


async def test_rules_page_renders(database) -> None:
    html = _html(await dashboard.dashboard_rules(_request("/dashboard/rules")))

    assert "90%" in html


async def test_pods_page_renders_container_states(mocker) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="srv-7-5d8f7c9b4-x2kqp", namespace="idegym", labels={}, deletion_timestamp=None),
        spec=SimpleNamespace(node_name="node-1"),
        status=SimpleNamespace(
            phase="Running",
            start_time=started,
            container_statuses=[
                SimpleNamespace(
                    name="server",
                    ready=False,
                    restart_count=3,
                    image="registry.example.com/env:latest",
                    state=SimpleNamespace(
                        running=None, waiting=SimpleNamespace(reason="CrashLoopBackOff"), terminated=None
                    ),
                    last_state=SimpleNamespace(
                        running=None,
                        waiting=None,
                        terminated=SimpleNamespace(
                            reason="OOMKilled", started_at=started, finished_at=started, exit_code=137
                        ),
                    ),
                )
            ],
        ),
    )
    core = mocker.Mock()
    core.list_namespaced_pod = mocker.AsyncMock(return_value=SimpleNamespace(items=[pod], metadata=None))

    @asynccontextmanager
    async def kube():
        yield None, None, core, None, None

    mocker.patch.object(dashboard, "async_kube_api", kube)

    html = _html(await dashboard.dashboard_pods(_request("/dashboard/pods")))

    assert "CrashLoopBackOff" in html
    assert "OOMKilled" in html
    assert "exit code 137" in html
    assert html.count("exit code") == 1  # only the terminated state has one; the waiting state must not


async def test_overview_tolerates_missing_resource_values(database, mocker) -> None:
    mocker.patch.object(
        dashboard, "get_running_idegym_servers", mocker.AsyncMock(return_value=[_server(cpu=None, ram=None)])
    )
    mocker.patch.object(dashboard, "get_alive_clients", mocker.AsyncMock(return_value=[_client(nodes_count=None)]))

    html = _html(await dashboard.root_page(_request()))

    assert "0.0 CPU" in html


async def test_a_failing_page_renders_the_error_page(mocker) -> None:
    @asynccontextmanager
    async def broken():
        raise RuntimeError("database is down")
        yield

    mocker.patch.object(dashboard, "get_db_session", broken)

    response = await dashboard.dashboard_servers(_request("/dashboard/servers"))

    assert response.status_code == 500
    assert "database is down" in response.body.decode()


@pytest.mark.parametrize(
    ("used", "limit", "level"),
    [
        (0, 10, "ok"),
        (7.4, 10, "ok"),
        (7.5, 10, "warning"),
        (9, 10, "critical"),
        (12, 10, "critical"),
        (1, 0, "critical"),
    ],
)
def test_usage_bands(used: float, limit: float, level: str) -> None:
    assert usage(used, limit)["level"] == level
    assert 0 <= usage(used, limit)["width"] <= 100


def test_status_levels_cover_every_availability() -> None:
    assert status_level(AvailabilityStatus.ALIVE) == "good"
    assert status_level(AvailabilityStatus.CRASHED) == "critical"
    assert all(
        status_level(status) != "neutral" for status in AvailabilityStatus if status != AvailabilityStatus.STOPPED
    )
    assert status_level("something new") == "neutral"


def test_timestamps_accept_milliseconds_and_datetimes() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    assert format_ts(int(moment.timestamp() * 1000)) == "2026-01-02 03:04:05"
    assert format_ts(moment) == "2026-01-02 03:04:05"
    assert iso_ts(moment) == "2026-01-02T03:04:05+00:00"
    assert format_ts(None) == ""


def test_timestamps_survive_values_out_of_range() -> None:
    assert format_ts(10**30) == str(10**30)
    assert iso_ts(10**30) == ""
