"""The dashboard's state-changing actions.

Handlers are called directly with the orchestrator's operations mocked out; what matters here is
the gatekeeping (the switch, the origin check), that each action reaches the same code path the API
uses with the identifiers from the database row, and that every outcome lands back on a page as a
notice rather than as a raw error.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi import HTTPException
from idegym.api.config import Config
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.orchestrator.router import dashboard, dashboard_actions
from kubernetes_asyncio.client import ApiException
from starlette.requests import Request
from structlog.testing import capture_logs

HOST = "idegym.example.com"


def _request(
    path: str = "/dashboard/servers/7/stop",
    enabled: bool = True,
    origin: Optional[str] = f"https://{HOST}",
    headers: Optional[dict[str, str]] = None,
    method: str = "POST",
) -> Request:
    config = Config()
    config.orchestrator.dashboard.actions_enabled = enabled
    raw = {"host": HOST, **(headers or {})}
    if origin is not None:
        raw["origin"] = origin
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(key.encode(), value.encode()) for key, value in raw.items()],
            "query_string": b"",
            "scheme": "https",
            "server": (HOST, 443),
            "client": ("testclient", 50000),
            "app": SimpleNamespace(state=SimpleNamespace(config=config)),
        }
    )


def _notice(response) -> tuple[str, str, str]:
    """Where a redirect goes, and the notice and level it carries."""
    assert response.status_code == 303, getattr(response, "body", b"").decode()
    location = urlparse(response.headers["location"])
    query = parse_qs(location.query)
    return location.path, query["notice"][0], query["level"][0]


def _server(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": 7,
        "client_id": uuid4(),
        "generated_name": "srv-7",
        "namespace": "sandboxes",
        "availability": AvailabilityStatus.ALIVE,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Db:
    """Stands in for a session: only the regex-clash lookup in ``_rule_problem`` touches it directly."""

    def __init__(self, clash: Any = None):
        self.clash = clash

    async def execute(self, statement: Any) -> SimpleNamespace:
        return SimpleNamespace(scalar=lambda: self.clash)


@pytest.fixture
def db(mocker):
    session = _Db()

    @asynccontextmanager
    async def open_session():
        yield session

    mocker.patch.object(dashboard_actions, "get_db_session", open_session)
    return session


# ---- Gatekeeping ---------------------------------------------------------------------------------


async def test_actions_are_404_until_enabled(db) -> None:
    response = await dashboard_actions.stop_server_action(_request(enabled=False), server_id=7)

    assert response.status_code == 404
    assert "IDEGYM_DASHBOARD_ACTIONS_ENABLED" in response.body.decode()


@pytest.mark.parametrize(
    "origin", [None, "null", "https://evil.example.com", f"https://{HOST}.evil.example.com"], ids=str
)
async def test_actions_from_another_origin_are_refused(db, origin: Optional[str]) -> None:
    response = await dashboard_actions.stop_server_action(_request(origin=origin), server_id=7)

    assert response.status_code == 403


def test_the_referer_stands_in_for_a_missing_origin() -> None:
    request = _request(origin=None, headers={"referer": f"https://{HOST}/dashboard/servers/7"})

    assert dashboard_actions.same_origin(request)


def test_a_proxy_forwarded_host_counts_as_ours() -> None:
    request = _request(
        origin="https://public.example.com", headers={"x-forwarded-host": "public.example.com, internal"}
    )

    assert dashboard_actions.same_origin(request)


def test_the_acting_user_comes_from_the_proxy_headers() -> None:
    assert dashboard_actions.acting_user(_request(headers={"x-auth-request-email": "ada@example.com"})) == (
        "ada@example.com"
    )
    assert dashboard_actions.acting_user(_request()) == "unknown"


# ---- Servers and clients -------------------------------------------------------------------------


async def test_stop_goes_through_the_regular_stop_operation(db, mocker) -> None:
    server = _server()
    mocker.patch.object(dashboard_actions, "get_idegym_server", mocker.AsyncMock(return_value=server))
    stop = mocker.patch.object(
        dashboard_actions, "stop_server_request", mocker.AsyncMock(return_value=SimpleNamespace(operation_id=41))
    )

    with capture_logs() as logs:
        response = await dashboard_actions.stop_server_action(
            _request(headers={"x-forwarded-email": "ada@example.com"}), server_id=7
        )

    (request,) = stop.await_args.args
    assert (request.client_id, request.server_id, request.namespace) == (server.client_id, 7, "sandboxes")
    path, notice, level = _notice(response)
    assert path == "/dashboard/servers/7"
    assert "operation 41" in notice
    assert level == "good"
    (entry,) = [log for log in logs if log.get("action") == "stop_server"]
    assert entry["user"] == "ada@example.com"


async def test_a_refused_stop_is_shown_on_the_server_page(db, mocker) -> None:
    mocker.patch.object(dashboard_actions, "get_idegym_server", mocker.AsyncMock(return_value=_server()))
    mocker.patch.object(
        dashboard_actions,
        "stop_server_request",
        mocker.AsyncMock(side_effect=HTTPException(status_code=409, detail="server is not available")),
    )

    _, notice, level = _notice(await dashboard_actions.stop_server_action(_request(), server_id=7))

    assert notice == "server is not available"
    assert level == "critical"


async def test_an_unexpected_failure_becomes_a_notice(db, mocker) -> None:
    mocker.patch.object(dashboard_actions, "get_idegym_server", mocker.AsyncMock(return_value=_server()))
    mocker.patch.object(dashboard_actions, "stop_server_request", mocker.AsyncMock(side_effect=RuntimeError("boom")))

    path, notice, level = _notice(await dashboard_actions.stop_server_action(_request(), server_id=7))

    assert path == "/dashboard/servers/7"
    assert notice == "RuntimeError: boom"
    assert level == "critical"


async def test_stopping_a_missing_server_says_so(db, mocker) -> None:
    mocker.patch.object(dashboard_actions, "get_idegym_server", mocker.AsyncMock(return_value=None))

    path, notice, _ = _notice(await dashboard_actions.stop_server_action(_request(), server_id=7))

    assert path == "/dashboard/servers"
    assert "does not exist" in notice


async def test_restart_passes_the_orchestrator_config(db, mocker) -> None:
    mocker.patch.object(dashboard_actions, "get_idegym_server", mocker.AsyncMock(return_value=_server()))
    restart = mocker.patch.object(
        dashboard_actions, "restart_server_with_config", mocker.AsyncMock(return_value=SimpleNamespace(operation_id=5))
    )
    request = _request("/dashboard/servers/7/restart")

    _, notice, _ = _notice(await dashboard_actions.restart_server_action(request, server_id=7))

    assert restart.await_args.kwargs["config"] is request.app.state.config
    assert "operation 5" in notice


async def test_stop_client_uses_the_clients_namespace(db, mocker) -> None:
    client = SimpleNamespace(id=uuid4(), name="team-alpha", namespace="sandboxes")
    mocker.patch.object(dashboard_actions, "get_client", mocker.AsyncMock(return_value=client))
    stop = mocker.patch.object(
        dashboard_actions, "stop_client", mocker.AsyncMock(return_value=SimpleNamespace(operation_id=9))
    )

    path, notice, _ = _notice(await dashboard_actions.stop_client_action(_request(), client_id=client.id))

    (request,) = stop.await_args.args
    assert (request.client_id, request.namespace) == (client.id, "sandboxes")
    assert path == "/dashboard/clients"
    assert "team-alpha" in notice


# ---- Rules ---------------------------------------------------------------------------------------


@pytest.fixture
def rules(db, mocker):
    catch_all = SimpleNamespace(id=1, client_name_regex=".*")
    team = SimpleNamespace(id=2, client_name_regex="^team-")
    saved = SimpleNamespace(id=3, client_name_regex="^new-")
    mocker.patch.object(
        dashboard_actions,
        "get_resource_limit_rule",
        mocker.AsyncMock(side_effect=lambda session, rule_id: {1: catch_all, 2: team}.get(rule_id)),
    )
    mocker.patch.object(dashboard_actions, "regex_error", mocker.AsyncMock(return_value=None))
    return SimpleNamespace(
        save=mocker.patch.object(dashboard_actions, "save_resource_limit_rule", mocker.AsyncMock(return_value=saved)),
        delete=mocker.patch.object(
            dashboard_actions, "delete_resource_limit_rule", mocker.AsyncMock(return_value=True)
        ),
        db=db,
    )


def _create(**overrides: Any) -> dict[str, Any]:
    values = {"client_name_regex": " ^new- ", "priority": 5, "pods_limit": 4, "cpu_limit": 8.0, "ram_limit": 16.0}
    values.update(overrides)
    return values


async def test_creating_a_rule_saves_the_trimmed_regex(rules) -> None:
    _, notice, level = _notice(await dashboard_actions.create_rule_action(_request("/dashboard/rules"), **_create()))

    assert rules.save.await_args.args[1:] == (None, "^new-", 4, 8.0, 16.0, 5)
    assert level == "good"
    assert "Created rule 3" in notice


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"client_name_regex": "  "}, "cannot be empty"),
        ({"pods_limit": -1}, "cannot be negative"),
        ({"cpu_limit": -0.5}, "cannot be negative"),
    ],
)
async def test_invalid_rules_are_rejected(rules, overrides: dict[str, Any], problem: str) -> None:
    _, notice, level = _notice(
        await dashboard_actions.create_rule_action(_request("/dashboard/rules"), **_create(**overrides))
    )

    assert problem in notice
    assert level == "critical"
    rules.save.assert_not_awaited()


async def test_postgres_decides_whether_a_regex_is_valid(rules, mocker) -> None:
    mocker.patch.object(dashboard_actions, "regex_error", mocker.AsyncMock(return_value="invalid regular expression"))

    _, notice, _ = _notice(await dashboard_actions.create_rule_action(_request("/dashboard/rules"), **_create()))

    assert "PostgreSQL rejects the regex: invalid regular expression" in notice
    rules.save.assert_not_awaited()


async def test_a_regex_another_rule_uses_is_rejected(rules) -> None:
    rules.db.clash = SimpleNamespace(id=2)

    _, notice, _ = _notice(await dashboard_actions.create_rule_action(_request("/dashboard/rules"), **_create()))

    assert "Rule 2 already uses that regex" in notice


async def test_a_rule_may_keep_its_own_regex(rules) -> None:
    rules.db.clash = SimpleNamespace(id=2)

    _, _, level = _notice(
        await dashboard_actions.update_rule_action(
            _request("/dashboard/rules/2"), rule_id=2, **_create(client_name_regex="^team-")
        )
    )

    assert level == "good"
    assert rules.save.await_args.args[1] == 2


async def test_the_catch_all_keeps_its_regex(rules) -> None:
    _, notice, _ = _notice(
        await dashboard_actions.update_rule_action(_request("/dashboard/rules/1"), rule_id=1, **_create())
    )

    assert "catch-all" in notice
    rules.save.assert_not_awaited()


async def test_the_catch_all_limits_can_change(rules) -> None:
    _, _, level = _notice(
        await dashboard_actions.update_rule_action(
            _request("/dashboard/rules/1"), rule_id=1, **_create(client_name_regex=".*", pods_limit=100)
        )
    )

    assert level == "good"


async def test_the_catch_all_cannot_be_deleted(rules) -> None:
    _, notice, _ = _notice(await dashboard_actions.delete_rule_action(_request("/dashboard/rules/1/delete"), rule_id=1))

    assert "cannot be deleted" in notice
    rules.delete.assert_not_awaited()


async def test_deleting_a_rule(rules) -> None:
    _, notice, level = _notice(
        await dashboard_actions.delete_rule_action(_request("/dashboard/rules/2/delete"), rule_id=2)
    )

    rules.delete.assert_awaited_once()
    assert level == "good"
    assert "Deleted rule 2" in notice


# ---- Consistency repairs -------------------------------------------------------------------------


async def test_recalculation_reports_how_many_counters_changed(mocker) -> None:
    session = SimpleNamespace(commit=mocker.AsyncMock())

    @asynccontextmanager
    async def open_session():
        yield session

    mocker.patch.object(dashboard_actions, "get_db_session", open_session)
    mocker.patch.object(
        dashboard_actions,
        "recalculate_rule_usage",
        mocker.AsyncMock(return_value={1: ((1, 1.0, 1.0), (1, 1.0, 1.0)), 2: ((3, 3.0, 3.0), (1, 1.0, 1.0))}),
    )

    _, notice, _ = _notice(await dashboard_actions.recalculate_action(_request("/dashboard/health/recalculate")))

    session.commit.assert_awaited_once()
    assert notice == "Rebuilt the usage counters of 2 rules; 1 changed."


@pytest.fixture
def orphan(db, mocker):
    deployment = SimpleNamespace(
        metadata=SimpleNamespace(
            labels={"app.kubernetes.io/part-of": "idegym", "app.kubernetes.io/component": "sandbox"}
        )
    )
    apps = SimpleNamespace(read_namespaced_deployment=mocker.AsyncMock(return_value=deployment))

    @asynccontextmanager
    async def clients():
        yield apps, None, None, None, None

    mocker.patch.object(dashboard_actions, "async_kube_api", clients)
    mocker.patch.object(dashboard_actions, "get_idegym_server_by_generated_name", mocker.AsyncMock(return_value=None))
    cleanup = mocker.patch.object(dashboard_actions, "clean_up_server", mocker.AsyncMock())
    return SimpleNamespace(deployment=deployment, apps=apps, cleanup=cleanup)


def _delete_orphan():
    return dashboard_actions.delete_orphan_action(
        _request("/dashboard/health/orphans/delete"), namespace="idegym", name="ghost-1"
    )


async def test_an_orphan_deployment_is_deleted_with_what_it_owns(orphan) -> None:
    _, notice, level = _notice(await _delete_orphan())

    orphan.cleanup.assert_awaited_once_with(name="ghost-1", namespace="idegym")
    assert level == "good"
    assert "ghost-1" in notice


async def test_a_deployment_a_live_server_expects_is_not_deleted(orphan, mocker) -> None:
    mocker.patch.object(
        dashboard_actions, "get_idegym_server_by_generated_name", mocker.AsyncMock(return_value=_server())
    )

    _, notice, level = _notice(await _delete_orphan())

    orphan.cleanup.assert_not_awaited()
    assert level == "critical"
    assert "belongs to server 7" in notice


async def test_only_idegym_server_deployments_are_deleted(orphan) -> None:
    orphan.deployment.metadata.labels = {"app": "postgres"}

    _, notice, _ = _notice(await _delete_orphan())

    orphan.cleanup.assert_not_awaited()
    assert "not an IdeGYM server Deployment" in notice


async def test_an_orphan_that_is_already_gone_is_fine(orphan) -> None:
    orphan.apps.read_namespaced_deployment.side_effect = ApiException(status=404, reason="Not Found")

    _, notice, level = _notice(await _delete_orphan())

    orphan.cleanup.assert_not_awaited()
    assert "already gone" in notice
    assert level == "good"


# ---- Pages ---------------------------------------------------------------------------------------


@pytest.fixture
def pages(mocker):
    @asynccontextmanager
    async def open_session():
        yield object()

    mocker.patch.object(dashboard, "get_db_session", open_session)
    rule = SimpleNamespace(
        id=1,
        client_name_regex=".*",
        priority=0,
        pods_limit=10,
        current_pods=1,
        cpu_limit=16.0,
        used_cpu=1.0,
        ram_limit=32.0,
        used_ram=2.0,
    )
    mocker.patch.object(dashboard, "_all_rules", mocker.AsyncMock(return_value=[rule]))


async def test_the_rules_page_offers_editing_only_when_enabled(pages) -> None:
    enabled = (await dashboard.dashboard_rules(_request("/dashboard/rules", method="GET"))).body.decode()
    disabled = (
        await dashboard.dashboard_rules(_request("/dashboard/rules", enabled=False, method="GET"))
    ).body.decode()

    assert "New rule" in enabled
    assert 'id="confirm-dialog"' in enabled
    assert 'action="/dashboard/rules/1"' in enabled
    assert "/dashboard/rules/1/delete" not in enabled  # the catch-all cannot be deleted
    assert "readonly" in enabled
    assert "New rule" not in disabled
    assert "confirm-dialog" not in disabled


async def test_a_notice_is_shown_on_the_page_it_redirects_to(pages) -> None:
    request = _request("/dashboard/rules", method="GET")
    request.scope["query_string"] = b"notice=Deleted+rule+2&level=good"

    html = (await dashboard.dashboard_rules(request)).body.decode()

    assert "Deleted rule 2" in html
    assert "callout-good" in html


async def test_deleting_the_deployment_of_a_deletion_failed_server_releases_its_quota(orphan, mocker) -> None:
    owner = _server(availability=AvailabilityStatus.DELETION_FAILED)
    mocker.patch.object(dashboard_actions, "get_idegym_server_by_generated_name", mocker.AsyncMock(return_value=owner))
    settle = mocker.patch.object(dashboard_actions, "settle_deletion_failed_server", mocker.AsyncMock())

    _, notice, _ = _notice(await _delete_orphan())

    orphan.cleanup.assert_awaited_once()
    assert settle.await_args.args[1] == owner.id
    assert "quota released" in notice


async def test_the_deployment_of_an_already_stopped_server_needs_no_release(orphan, mocker) -> None:
    owner = _server(availability=AvailabilityStatus.STOPPED)
    mocker.patch.object(dashboard_actions, "get_idegym_server_by_generated_name", mocker.AsyncMock(return_value=owner))
    settle = mocker.patch.object(dashboard_actions, "settle_deletion_failed_server", mocker.AsyncMock())

    await _delete_orphan()

    orphan.cleanup.assert_awaited_once()
    settle.assert_not_awaited()
