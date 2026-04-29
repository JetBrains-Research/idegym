from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from fastmcp.exceptions import ToolError
from idegym.api.orchestrator.mcp import MCPToolName
from idegym.orchestrator.main import create_app
from idegym.orchestrator.mcp import create_mcp_server
from starlette.datastructures import Headers
from starlette.routing import Mount

EXPECTED_MCP_TOOLS = {
    MCPToolName.REGISTER_CLIENT,
    MCPToolName.STOP_CLIENT,
    MCPToolName.FINISH_CLIENT,
    MCPToolName.START_SERVER,
    MCPToolName.STOP_SERVER,
    MCPToolName.FINISH_SERVER,
    MCPToolName.RESTART_SERVER,
    MCPToolName.BUILD_IMAGES_FROM_YAML,
    MCPToolName.GET_OPERATION_STATUS,
    MCPToolName.GET_JOB_STATUS,
    MCPToolName.FORWARD_REQUEST,
    MCPToolName.RUN_BASH_COMMAND,
}


def test_orchestrator_mounts_mcp_app():
    app = create_app()

    route_paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    mount_paths = {route.path for route in app.routes if isinstance(route, Mount)}

    assert "/health" in route_paths
    assert "/mcp" in mount_paths


async def test_create_mcp_server_without_orchestrator_startup():
    mcp = create_mcp_server()

    tools = await mcp.list_tools()

    assert {tool.name for tool in tools} == EXPECTED_MCP_TOOLS
    assert all(tool.parameters["required"] == ["request"] for tool in tools)


async def test_start_server_mcp_tool_schema_reuses_start_server_request_for_agents():
    mcp = create_mcp_server()

    tools = await mcp.list_tools()
    start_server_tool = next(tool for tool in tools if tool.name == MCPToolName.START_SERVER)

    assert start_server_tool.description == "Start a server pod from an OCI image or reuse a matching finished server."
    assert start_server_tool.parameters["required"] == ["request"]
    request_schema = start_server_tool.parameters["properties"]["request"]
    assert request_schema["required"] == ["client_id", "image_tag"]
    assert request_schema["properties"]["image_tag"]["examples"] == ["registry.example.com/my-env:latest"]
    assert request_schema["properties"]["runtime_class_name"]["examples"] == ["gvisor"]
    assert request_schema["properties"]["resources"]["examples"] == [
        {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}}
    ]
    assert request_schema["properties"]["reuse_strategy"]["enum"] == ["NONE", "RESTART", "RESET", "CHECKPOINT"]


async def test_register_client_mcp_tool_calls_endpoint(mocker):
    client_id = uuid4()
    endpoint = mocker.patch(
        "idegym.orchestrator.mcp.register_client_with_node_pool",
        return_value={
            "id": str(client_id),
            "name": "mcp-client",
            "nodes_count": 0,
            "namespace": "idegym",
            "last_heartbeat_time": 1,
            "availability": "ALIVE",
            "created_at": 1,
            "operation_id": None,
        },
    )
    config = SimpleNamespace(orchestrator=SimpleNamespace(node_pool=object()))
    mcp = create_mcp_server(config=config)

    result = await mcp.call_tool(MCPToolName.REGISTER_CLIENT, {"request": {"name": "mcp-client"}})

    endpoint.assert_awaited_once()
    request = endpoint.await_args.kwargs["request"]
    assert request.model_dump(mode="json") == {"name": "mcp-client", "nodes_count": 0, "namespace": "idegym"}
    assert result.structured_content["id"] == str(client_id)
    assert result.structured_content["operation_id"] is None


async def test_register_client_mcp_tool_requires_orchestrator_config():
    mcp = create_mcp_server()

    with pytest.raises(ToolError, match="requires orchestrator configuration"):
        await mcp.call_tool(MCPToolName.REGISTER_CLIENT, {"request": {"name": "mcp-client"}})


async def test_start_server_mcp_tool_calls_endpoint(mocker):
    client_id = uuid4()
    endpoint = mocker.patch(
        "idegym.orchestrator.mcp.start_server_with_config",
        return_value={"namespace": "idegym", "client_id": str(client_id), "operation_id": 42},
    )
    config = object()
    mcp = create_mcp_server(config=config)

    result = await mcp.call_tool(
        MCPToolName.START_SERVER,
        {
            "request": {
                "client_id": str(client_id),
                "image_tag": "registry.example.com/idegym/server:test",
                "namespace": "custom",
                "server_name": "agent-server",
                "service_port": 8080,
                "container_port": 9000,
                "reuse_strategy": "NONE",
                "server_kind": "openenv",
            },
        },
    )

    endpoint.assert_awaited_once()
    request = endpoint.await_args.kwargs["request"]
    assert request.client_id == client_id
    assert request.namespace == "custom"
    assert request.image_tag == "registry.example.com/idegym/server:test"
    assert request.server_name == "agent-server"
    assert request.service_port == 8080
    assert request.container_port == 9000
    assert request.reuse_strategy == "NONE"
    assert request.server_kind == "openenv"
    assert result.structured_content == {"namespace": "idegym", "client_id": str(client_id), "operation_id": 42}


async def test_run_bash_command_mcp_tool_calls_forwarding_endpoint(mocker):
    client_id = uuid4()
    endpoint = mocker.patch(
        "idegym.orchestrator.mcp.forward_request_to_server",
        return_value={"async_operation_id": 43},
    )
    mcp = create_mcp_server(get_http_client=lambda: object())

    result = await mcp.call_tool(
        MCPToolName.RUN_BASH_COMMAND,
        {
            "request": {
                "client_id": str(client_id),
                "server_id": 7,
                "command": "echo hello",
            },
        },
    )

    endpoint.assert_awaited_once()
    assert endpoint.await_args.kwargs["client_id"] == client_id
    assert endpoint.await_args.kwargs["server_id"] == 7
    assert endpoint.await_args.kwargs["path"] == "api/tools/bash"
    assert isinstance(endpoint.await_args.kwargs["headers"], Headers)
    assert endpoint.await_args.kwargs["headers"]["content-type"] == "application/json"
    assert endpoint.await_args.kwargs["body"] == (
        '{"command":"echo hello","timeout":600.0,"graceful_termination_timeout":2.0}'
    )
    assert result.structured_content == {"async_operation_id": 43}


async def test_run_bash_command_mcp_tool_requires_http_client():
    client_id = uuid4()
    mcp = create_mcp_server()

    with pytest.raises(ToolError, match="requires the orchestrator HTTP client"):
        await mcp.call_tool(
            MCPToolName.RUN_BASH_COMMAND,
            {
                "request": {
                    "client_id": str(client_id),
                    "server_id": 7,
                    "command": "echo hello",
                },
            },
        )
