"""Reporting whether a start actually reused a server.

Reuse turns on seven fields matching, and the common way to get it wrong — always calling
``stop_server``, so nothing is ever FINISHED — is invisible. These pin that the answer is
reported rather than left to be inferred.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from idegym.api.orchestrator.operations import AsyncOperationStatus
from idegym.api.orchestrator.servers import (
    ServerReuseStrategy,
    StartServerRequest,
    StartServerResponse,
)
from idegym.orchestrator.router import server as server_router


def _request(**overrides) -> StartServerRequest:
    fields = {
        "client_id": uuid4(),
        "image_tag": "registry.test/env:latest",
        "server_name": "srv",
        "reuse_strategy": ServerReuseStrategy.RESET,
    }
    fields.update(overrides)
    return StartServerRequest(**fields)


@pytest.fixture
def started(mocker):
    """Run `_task_start_server` with everything it touches stubbed; return the recorded result."""

    async def run(request, existing=None):
        mocker.patch.object(server_router, "extract_resources_request", return_value=(1.0, 1.0))
        mocker.patch.object(
            server_router,
            "find_matching_finished_server_in_db",
            mocker.AsyncMock(return_value=(existing, "client-name")),
        )
        mocker.patch.object(server_router, "validate_client", mocker.AsyncMock(return_value=SimpleNamespace(name="c")))
        mocker.patch.object(
            server_router,
            "check_resources_and_save_server_in_db",
            mocker.AsyncMock(
                return_value=SimpleNamespace(id=1, generated_name="srv-new", server_name="srv", image_tag="tag")
            ),
        )
        for name in ("deploy_server", "wait_for_pods_ready", "restart_pods", "update_server_owner"):
            mocker.patch.object(server_router, name, mocker.AsyncMock())
        mocker.patch.object(server_router, "update_server_status", mocker.AsyncMock())
        update = mocker.patch.object(server_router, "update_operation_status", mocker.AsyncMock())

        await server_router._task_start_server(config=mocker.MagicMock(), request=request, async_operation_id=1)

        succeeded = [
            call
            for call in update.await_args_list
            if call.kwargs.get("async_operation_status") == AsyncOperationStatus.SUCCEEDED
        ]
        assert succeeded, "the start task did not report success"
        return succeeded[-1].kwargs["result"]

    return run


def _existing_server():
    return SimpleNamespace(id=9, generated_name="srv-old", server_name="srv", image_tag="tag")


async def test_a_fresh_start_reports_no_reuse(started) -> None:
    result = await started(_request())

    assert result.reused is False


async def test_taking_over_a_finished_server_reports_reuse(started) -> None:
    result = await started(_request(), existing=_existing_server())

    assert result.reused is True
    assert result.server_id == 9


async def test_a_restart_reuse_is_reported_without_asking_for_a_reset(started) -> None:
    result = await started(_request(reuse_strategy=ServerReuseStrategy.RESTART), existing=_existing_server())

    assert (result.reused, result.need_to_reset) == (True, False)


async def test_reuse_is_not_attempted_at_all_for_strategy_none(started, mocker) -> None:
    result = await started(_request(reuse_strategy=ServerReuseStrategy.NONE), existing=_existing_server())

    assert result.reused is False
    server_router.find_matching_finished_server_in_db.assert_not_awaited()


def test_the_response_defaults_to_not_reused() -> None:
    response = StartServerResponse(namespace="idegym", client_id=uuid4())

    assert response.reused is False


def test_the_match_key_is_written_down_on_reuse_strategy() -> None:
    """The field description is the only place a caller can learn what reuse matches on."""
    description = StartServerRequest.model_fields["reuse_strategy"].description

    for field in ("image_tag", "runtime_class_name", "run_as_root", "server_kind", "server_name", "FINISHED"):
        assert field in description, field
