from types import SimpleNamespace
from uuid import uuid4

import pytest
from idegym.api.config import Config, SameImageAffinityConfig
from idegym.api.orchestrator.servers import ServerReuseStrategy, StartServerRequest
from idegym.orchestrator.router import server as server_router

pytestmark = pytest.mark.unit


async def test_fresh_server_passes_same_image_affinity_config_to_deployment(mocker):
    config = Config()
    config.orchestrator.same_image_affinity = SameImageAffinityConfig(enabled=True, preference_weight=73)
    request = StartServerRequest(
        client_id=uuid4(),
        image_tag="registry.example/task:123",
        reuse_strategy=ServerReuseStrategy.NONE,
    )
    mocker.patch.object(server_router, "extract_resources_request", return_value=(1.0, 2.0))
    mocker.patch.object(
        server_router,
        "validate_client",
        new=mocker.AsyncMock(return_value=SimpleNamespace(name="client")),
    )
    mocker.patch.object(
        server_router,
        "check_resources_and_save_server_in_db",
        new=mocker.AsyncMock(
            return_value=SimpleNamespace(
                id=7,
                generated_name="server-7",
                server_name="default-idegym-server",
                image_tag=request.image_tag,
            )
        ),
    )
    deploy_server = mocker.patch.object(server_router, "deploy_server", new=mocker.AsyncMock())
    mocker.patch.object(server_router, "wait_for_pods_ready", new=mocker.AsyncMock())
    mocker.patch.object(server_router, "update_server_status", new=mocker.AsyncMock())
    mocker.patch.object(server_router, "update_operation_status", new=mocker.AsyncMock())

    await server_router._task_start_server(config=config, request=request, async_operation_id=11)

    assert deploy_server.await_args.kwargs["same_image_affinity_enabled"] is True
    assert deploy_server.await_args.kwargs["same_image_affinity_preference_weight"] == 73
