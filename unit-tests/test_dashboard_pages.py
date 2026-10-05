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
from idegym.orchestrator.database.database import RuleUsage
from idegym.orchestrator.router import dashboard
from idegym.orchestrator.templating import format_ts, iso_ts, status_level, usage
from kubernetes_asyncio.client import ApiException
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


def _pod(name: str = "srv-7-5d8f7c9b4-x2kqp", app: str = "srv-7", ready: bool = False) -> SimpleNamespace:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="idegym", labels={"app": app}, deletion_timestamp=None),
        spec=SimpleNamespace(
            node_name="node-1",
            containers=[SimpleNamespace(name="server")],
            init_containers=[SimpleNamespace(name="setup")],
        ),
        status=SimpleNamespace(
            phase="Running",
            start_time=started,
            container_statuses=[
                SimpleNamespace(
                    name="server",
                    ready=ready,
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


def _event(name: str, kind: str = "Pod", type: str = "Normal", reason: str = "Pulled", minute: int = 0):
    return SimpleNamespace(
        type=type,
        reason=reason,
        message=f"{reason} for {name}",
        count=2,
        series=None,
        source=SimpleNamespace(component="kubelet"),
        reporting_component=None,
        involved_object=SimpleNamespace(kind=kind, name=name, namespace="idegym"),
        last_timestamp=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
        event_time=None,
        first_timestamp=None,
        metadata=SimpleNamespace(namespace="idegym", creation_timestamp=None),
    )


@pytest.fixture
def kube(mocker):
    """Kubernetes clients whose calls each test configures; ``apps`` and ``core`` as in ``async_kube_api``."""
    apps, core = mocker.Mock(), mocker.Mock()
    core.list_namespaced_pod = mocker.AsyncMock(return_value=SimpleNamespace(items=[_pod()], metadata=None))
    core.list_namespaced_event = mocker.AsyncMock(return_value=SimpleNamespace(items=[]))
    core.read_namespaced_pod = mocker.AsyncMock(return_value=_pod())
    core.read_namespaced_pod_log = mocker.AsyncMock(return_value="first line\nsecond <b>line</b>\n")
    apps.list_namespaced_replica_set = mocker.AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name="srv-7-5d8f7c9b4"))])
    )

    @asynccontextmanager
    async def clients():
        yield apps, None, core, None, None

    mocker.patch.object(dashboard, "async_kube_api", clients)
    mocker.patch.object(
        dashboard,
        "describe_pod_startup",
        mocker.AsyncMock(return_value="still pulling the image or creating the container (ContainerCreating)"),
    )
    return SimpleNamespace(apps=apps, core=core)


@pytest.fixture
def owners(mocker):
    return mocker.patch.object(
        dashboard, "get_idegym_servers_by_generated_names", mocker.AsyncMock(return_value=[_server()])
    )


async def test_pods_page_renders_container_states_and_owners(database, kube, owners) -> None:
    html = _html(await dashboard.dashboard_pods(_request("/dashboard/pods")))

    assert "CrashLoopBackOff" in html
    assert "OOMKilled" in html
    assert "exit code 137" in html
    assert html.count("exit code") == 1  # only the terminated state has one; the waiting state must not
    assert 'href="/dashboard/servers/7"' in html
    assert 'href="/dashboard/pods/idegym/srv-7-5d8f7c9b4-x2kqp"' in html


async def test_overview_tolerates_missing_resource_values(database, mocker) -> None:
    mocker.patch.object(
        dashboard, "get_running_idegym_servers", mocker.AsyncMock(return_value=[_server(cpu=None, ram=None)])
    )
    mocker.patch.object(dashboard, "get_alive_clients", mocker.AsyncMock(return_value=[_client(nodes_count=None)]))

    html = _html(await dashboard.root_page(_request()))

    assert "0.0 CPU" in html


async def test_pods_page_offers_the_namespaces_of_running_servers(database, kube, owners, mocker) -> None:
    mocker.patch.object(
        dashboard, "get_running_idegym_servers", mocker.AsyncMock(return_value=[_server(namespace="sandboxes")])
    )

    html = _html(await dashboard.dashboard_pods(_request("/dashboard/pods"), namespace="sandboxes"))

    kube.core.list_namespaced_pod.assert_awaited_once()
    assert kube.core.list_namespaced_pod.await_args.kwargs["namespace"] == "sandboxes"
    assert '<option value="idegym"' in html
    assert '<option value="sandboxes" selected' in html


async def test_servers_page_filters_by_status(database, mocker) -> None:
    recent = mocker.patch.object(
        dashboard,
        "get_recent_idegym_servers",
        mocker.AsyncMock(return_value=[_server(availability=AvailabilityStatus.CRASHED, details="OOMKilled")]),
    )

    html = _html(await dashboard.dashboard_servers(_request("/dashboard/servers"), status="CRASHED"))

    assert recent.await_args.kwargs["statuses"] == {AvailabilityStatus.CRASHED}
    assert "OOMKilled" in html
    assert '<option value="CRASHED" selected' in html


async def test_an_unknown_status_filter_falls_back_to_alive_servers(database, mocker) -> None:
    recent = mocker.patch.object(dashboard, "get_recent_idegym_servers", mocker.AsyncMock())

    _html(await dashboard.dashboard_servers(_request("/dashboard/servers"), status="nonsense"))

    recent.assert_not_awaited()
    dashboard.get_running_idegym_servers.assert_awaited()


@pytest.fixture
def server_page(database, kube, mocker):
    mocker.patch.object(dashboard, "get_idegym_server", mocker.AsyncMock(return_value=_server()))
    mocker.patch.object(dashboard, "find_matching_resource_limit_rule", mocker.AsyncMock(return_value=_rule()))
    operation = SimpleNamespace(
        id=11,
        request_type="START_SERVER",
        status="SUCCEEDED",
        scheduled_at=1_700_000_000_000,
        started_at=1_700_000_000_500,
        finished_at=1_700_000_004_500,
        server_id=7,
        orchestrator_pod="idegym-abc",
        result='{"message": "started"}',
    )
    mocker.patch.object(dashboard, "get_recent_async_operations", mocker.AsyncMock(return_value=[operation]))
    return kube


async def test_server_page_brings_pods_events_and_operations_together(server_page) -> None:
    server_page.core.list_namespaced_event.side_effect = lambda namespace, field_selector: SimpleNamespace(
        items=[_event(field_selector.split("=", 1)[1], type="Warning", reason="FailedCreate")]
        if field_selector.endswith("srv-7-5d8f7c9b4")
        else []
    )

    html = _html(await dashboard.dashboard_server(_request("/dashboard/servers/7"), server_id=7))

    selectors = [call.kwargs["field_selector"] for call in server_page.core.list_namespaced_event.await_args_list]
    assert selectors == [
        "involvedObject.name=srv-7",
        "involvedObject.name=srv-7-5d8f7c9b4",
        "involvedObject.name=srv-7-5d8f7c9b4-x2kqp",
    ]
    assert "FailedCreate" in html
    assert "START_SERVER" in html
    assert "4.0 s" in html
    assert "Still pulling the image or creating the container (ContainerCreating)." in html
    assert "app=srv-7" in html


async def test_server_page_survives_kubernetes_being_unreadable(server_page) -> None:
    server_page.core.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")

    html = _html(await dashboard.dashboard_server(_request("/dashboard/servers/7"), server_id=7))

    assert "Could not read the server" in html
    assert "START_SERVER" in html


async def test_a_missing_server_is_a_404(database, mocker) -> None:
    mocker.patch.object(dashboard, "get_idegym_server", mocker.AsyncMock(return_value=None))

    response = await dashboard.dashboard_server(_request("/dashboard/servers/404"), server_id=404)

    assert response.status_code == 404
    assert "Server 404 does not exist" in response.body.decode()


async def test_pod_page_shows_an_escaped_bounded_tail(database, kube, owners) -> None:
    html = _html(
        await dashboard.dashboard_pod(
            _request("/dashboard/pods/idegym/srv-7-5d8f7c9b4-x2kqp"),
            namespace="idegym",
            pod_name="srv-7-5d8f7c9b4-x2kqp",
            tail=123456,
        )
    )

    arguments = kube.core.read_namespaced_pod_log.await_args.kwargs
    assert arguments["tail_lines"] == 500  # not one of the offered sizes, so the default
    assert arguments["limit_bytes"] == dashboard.LOG_VIEW_BYTES
    assert arguments["container"] == "server"
    assert "second &lt;b&gt;line&lt;/b&gt;" in html
    assert '<optgroup label="Init containers">' in html
    assert 'href="/dashboard/servers/7"' in html


async def test_pod_page_reports_an_unreadable_log_in_place(database, kube, owners) -> None:
    kube.core.read_namespaced_pod_log.side_effect = ApiException(status=400, reason="Bad Request")
    kube.core.read_namespaced_pod_log.side_effect.body = '{"message": "previous terminated container not found"}'

    html = _html(
        await dashboard.dashboard_pod(
            _request("/dashboard/pods/idegym/p"), namespace="idegym", pod_name="p", previous=True
        )
    )

    assert "previous terminated container not found" in html


async def test_a_missing_pod_is_a_404(kube) -> None:
    kube.core.read_namespaced_pod.side_effect = ApiException(status=404, reason="Not Found")

    response = await dashboard.dashboard_pod(
        _request("/dashboard/pods/idegym/gone"), namespace="idegym", pod_name="gone"
    )

    assert response.status_code == 404


class _StreamedLog:
    """The raw aiohttp response ``read_namespaced_pod_log`` returns when not preloading."""

    def __init__(self, status: int, chunks: list[bytes], body: str = ""):
        self.status = status
        self.reason = "OK" if status == 200 else "Bad Request"
        self.released = False
        self._chunks = chunks
        self._body = body
        self.content = SimpleNamespace(iter_chunked=self._iterate)

    async def _iterate(self, size: int):
        for chunk in self._chunks:
            yield chunk

    async def text(self) -> str:
        return self._body

    def release(self) -> None:
        self.released = True


async def test_log_download_streams_the_capped_log_and_releases_the_connection(kube) -> None:
    streamed = _StreamedLog(200, [b"one\n", b"two\n"])
    kube.core.read_namespaced_pod_log.return_value = streamed

    response = await dashboard.download_pod_log(
        _request("/dashboard/pods/idegym/p/logs"), namespace="idegym", pod_name="p", container="server"
    )
    body = b"".join([chunk async for chunk in response.body_iterator])

    assert body == b"one\ntwo\n"
    assert streamed.released
    assert kube.core.read_namespaced_pod_log.await_args.kwargs["limit_bytes"] == dashboard.LOG_DOWNLOAD_BYTES
    assert kube.core.read_namespaced_pod_log.await_args.kwargs["_request_timeout"] == (30, 120)
    assert response.headers["content-disposition"] == 'attachment; filename="p-server.log"'


async def test_a_failed_log_download_renders_the_error_page(kube) -> None:
    streamed = _StreamedLog(400, [], body='{"message": "container not found"}')
    kube.core.read_namespaced_pod_log.return_value = streamed

    response = await dashboard.download_pod_log(
        _request("/dashboard/pods/idegym/p/logs"), namespace="idegym", pod_name="p"
    )

    assert response.status_code == 500
    assert "container not found" in response.body.decode()
    assert streamed.released


async def test_events_page_puts_warnings_first(kube) -> None:
    kube.core.list_namespaced_event.return_value = SimpleNamespace(
        items=[
            _event("a", reason="Pulled", minute=30),
            _event("b", type="Warning", reason="BackOff", minute=5),
            _event("c", reason="Started", minute=40),
        ]
    )

    html = _html(await dashboard.dashboard_events(_request("/dashboard/events")))

    assert html.index("BackOff") < html.index("Started") < html.index("Pulled")
    assert kube.core.list_namespaced_event.await_args.kwargs["field_selector"] is None


async def test_events_page_can_ask_kubernetes_for_warnings_only(kube) -> None:
    _html(await dashboard.dashboard_events(_request("/dashboard/events"), warnings=True))

    assert kube.core.list_namespaced_event.await_args.kwargs["field_selector"] == "type=Warning"


def test_event_time_falls_back_through_the_fields_each_api_version_fills() -> None:
    event = _event("a")
    event.last_timestamp = None
    event.series = SimpleNamespace(last_observed_time=datetime(2026, 2, 2, tzinfo=UTC), count=9)

    view = dashboard.event_view(event)

    assert view["last"] == datetime(2026, 2, 2, tzinfo=UTC)
    assert view["count"] == 2  # the event's own count wins over the series count


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


async def test_pod_pages_work_while_the_database_is_down(kube, mocker) -> None:
    @asynccontextmanager
    async def broken():
        raise RuntimeError("database is down")
        yield

    mocker.patch.object(dashboard, "get_db_session", broken)

    listing = _html(await dashboard.dashboard_pods(_request("/dashboard/pods")))
    page = _html(
        await dashboard.dashboard_pod(
            _request("/dashboard/pods/idegym/srv-7-5d8f7c9b4-x2kqp"),
            namespace="idegym",
            pod_name="srv-7-5d8f7c9b4-x2kqp",
        )
    )

    assert "CrashLoopBackOff" in listing
    assert "second &lt;b&gt;line&lt;/b&gt;" in page


async def test_operations_page_filters_by_status_and_type(database, mocker) -> None:
    recent = mocker.patch.object(dashboard, "get_recent_async_operations", mocker.AsyncMock(return_value=[]))

    html = _html(
        await dashboard.dashboard_operations(
            _request("/dashboard/operations"), status="FAILED", request_type="STOP_SERVER"
        )
    )

    assert recent.await_args.kwargs["statuses"] == {"FAILED"}
    assert recent.await_args.kwargs["request_types"] == {"STOP_SERVER"}
    assert '<option value="FAILED" selected' in html


async def test_operations_page_ignores_unknown_filters(database, mocker) -> None:
    recent = mocker.patch.object(dashboard, "get_recent_async_operations", mocker.AsyncMock(return_value=[]))

    _html(await dashboard.dashboard_operations(_request("/dashboard/operations"), status="nope", request_type="nope"))

    assert recent.await_args.kwargs["statuses"] is None
    assert recent.await_args.kwargs["request_types"] is None


async def test_builds_page_renders(database, mocker) -> None:
    build = SimpleNamespace(
        job_name="kaniko-abc",
        request_id="req-1",
        status="failure",
        tag="registry.example.com/env:1",
        created_at=1_700_000_000_000,
        updated_at=1_700_000_090_000,
        details="error building image",
    )
    mocker.patch.object(dashboard, "get_recent_job_statuses", mocker.AsyncMock(return_value=[build]))

    html = _html(await dashboard.dashboard_builds(_request("/dashboard/builds")))

    assert "kaniko-abc" in html
    assert "badge-critical" in html
    assert "90 s" in html


async def test_snapshots_page_renders(database, mocker) -> None:
    job = SimpleNamespace(
        job_id="job-1",
        prepare_request_id=None,
        status="success",
        snapshot_id=3,
        created_at=1,
        updated_at=2,
        details=None,
    )
    snapshot = SimpleNamespace(
        id=3,
        snapshot_name="snap-3",
        pod_snapshot_name=None,
        image_tag="registry.example.com/env:1",
        server_name="srv",
        namespace="idegym",
        server_kind="idegym",
        runtime_class_name=None,
        run_as_root=False,
        updated_at=2,
    )
    mocker.patch.object(dashboard, "get_recent_snapshot_jobs", mocker.AsyncMock(return_value=[job]))
    mocker.patch.object(dashboard, "get_recent_snapshots", mocker.AsyncMock(return_value=[snapshot]))

    html = _html(await dashboard.dashboard_snapshots(_request("/dashboard/snapshots")))

    assert "job-1" in html
    assert "snap-3" in html


async def test_health_page_shows_drift_and_orphans(database, kube, mocker) -> None:
    mocker.patch.object(dashboard, "_all_rules", mocker.AsyncMock(return_value=[_rule(current_pods=2)]))
    mocker.patch.object(dashboard, "recompute_rule_usage", mocker.AsyncMock(return_value={}))
    mocker.patch.object(dashboard, "get_idegym_servers_by_status", mocker.AsyncMock(return_value=[]))
    mocker.patch.object(dashboard, "get_idegym_servers_by_generated_names", mocker.AsyncMock(return_value=[]))
    ghost = SimpleNamespace(
        metadata=SimpleNamespace(
            name="ghost-1", creation_timestamp=datetime(2020, 1, 1, tzinfo=UTC), deletion_timestamp=None
        )
    )
    kube.apps.list_namespaced_deployment = mocker.AsyncMock(return_value=SimpleNamespace(items=[ghost]))

    html = _html(await dashboard.dashboard_health(_request("/dashboard/health")))

    selector = kube.apps.list_namespaced_deployment.await_args.kwargs["label_selector"]
    assert selector == "app.kubernetes.io/part-of=idegym,app.kubernetes.io/component=sandbox"
    assert "Drifting" in html
    assert "(+2)" in html
    assert "ghost-1" in html
    assert "No server row owns this Deployment" in html


async def test_health_page_reports_namespaces_it_cannot_list(database, kube, mocker) -> None:
    mocker.patch.object(dashboard, "recompute_rule_usage", mocker.AsyncMock(return_value={1: RuleUsage(9, 4.0, 31.0)}))
    mocker.patch.object(dashboard, "get_idegym_servers_by_status", mocker.AsyncMock(return_value=[]))
    mocker.patch.object(dashboard, "get_idegym_servers_by_generated_names", mocker.AsyncMock(return_value=[]))
    kube.apps.list_namespaced_deployment = mocker.AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))

    html = _html(await dashboard.dashboard_health(_request("/dashboard/health")))

    assert "Could not list Deployments" in html
