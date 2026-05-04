import pytest
from idegym.api.orchestrator.mcp import MCPToolName
from kubernetes_asyncio.client import V1ResourceRequirements
from utils.constants import DEFAULT_NAMESPACE, DEFAULT_SERVER_START_TIMEOUT
from utils.mcp_utils import (
    create_mcp_client,
    parse_forwarded_body,
    parse_operation_result,
    wait_for_mcp_operation,
)

REQUIRED_MCP_TOOLS = {
    MCPToolName.REGISTER_CLIENT,
    MCPToolName.START_SERVER,
    MCPToolName.GET_OPERATION_STATUS,
    MCPToolName.RUN_BASH_COMMAND,
    MCPToolName.FINISH_SERVER,
    MCPToolName.STOP_SERVER,
    MCPToolName.STOP_CLIENT,
    MCPToolName.FINISH_CLIENT,
}

REQUIRED_CLIENT_TOOL_ARGS = {
    MCPToolName.REGISTER_CLIENT: ["name"],
    MCPToolName.START_SERVER: ["client_id", "image_tag"],
    MCPToolName.GET_OPERATION_STATUS: ["operation_id"],
    MCPToolName.RUN_BASH_COMMAND: ["client_id", "server_id", "command"],
    MCPToolName.FINISH_SERVER: ["client_id", "server_id"],
    MCPToolName.STOP_SERVER: ["client_id", "server_id"],
    MCPToolName.STOP_CLIENT: ["client_id"],
    MCPToolName.FINISH_CLIENT: ["client_id"],
}


@pytest.mark.asyncio
async def test_mcp_transport_smoke(test_id):
    async with create_mcp_client() as mcp:
        assert await mcp.ping()

        tools = await mcp.list_tools()
        tools_by_name = {tool.name: tool for tool in tools}

        assert REQUIRED_MCP_TOOLS <= tools_by_name.keys()
        # FastMCP exposes single request-model tool arguments under a top-level request object.
        for tool_name, required_args in REQUIRED_CLIENT_TOOL_ARGS.items():
            input_schema = tools_by_name[tool_name].inputSchema
            assert input_schema["required"] == ["request"]
            assert input_schema["properties"]["request"]["required"] == required_args

        register_result = await mcp.call_tool(
            MCPToolName.REGISTER_CLIENT,
            {
                "request": {
                    "name": f"mcp-smoke-{test_id}",
                    "namespace": DEFAULT_NAMESPACE,
                    "nodes_count": 0,
                },
            },
        )
        client_id = register_result.structured_content["id"]
        assert client_id

        finish_result = await mcp.call_tool(
            MCPToolName.FINISH_CLIENT,
            {
                "request": {
                    "client_id": client_id,
                    "namespace": DEFAULT_NAMESPACE,
                },
            },
        )
        assert finish_result.structured_content["id"] == client_id
        assert finish_result.structured_content["availability"] == "FINISHED"


@pytest.mark.asyncio
async def test_mcp_start_server_and_run_bash_command(test_image, test_id):
    client_id = None
    server_id = None
    active_server_id = None
    server_name = f"mcp-lifecycle-{test_id}"

    async with create_mcp_client(timeout=900.0) as mcp:
        try:
            register_result = await mcp.call_tool(
                MCPToolName.REGISTER_CLIENT,
                {
                    "request": {
                        "name": f"mcp-lifecycle-{test_id}",
                        "namespace": DEFAULT_NAMESPACE,
                        "nodes_count": 0,
                    },
                },
            )
            client_id = register_result.structured_content["id"]

            resources = V1ResourceRequirements(
                requests={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                limits={"cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            ).to_dict()
            start_request = {
                "client_id": client_id,
                "namespace": DEFAULT_NAMESPACE,
                "image_tag": test_image,
                "server_name": server_name,
                "runtime_class_name": "gvisor",
                "run_as_root": True,
                "resources": resources,
                "server_start_wait_timeout_in_seconds": DEFAULT_SERVER_START_TIMEOUT,
                "reuse_strategy": "RESTART",
            }
            start_result = await mcp.call_tool(
                MCPToolName.START_SERVER,
                {"request": start_request},
            )
            start_operation_id = start_result.structured_content["operation_id"]
            start_status = await wait_for_mcp_operation(
                mcp,
                start_operation_id,
                timeout=DEFAULT_SERVER_START_TIMEOUT,
                poll_interval=2.0,
            )
            start_response = parse_operation_result(start_status)
            server_id = start_response["server_id"]
            active_server_id = server_id

            command_result = await mcp.call_tool(
                MCPToolName.RUN_BASH_COMMAND,
                {
                    "request": {
                        "client_id": client_id,
                        "server_id": server_id,
                        "command": "python -c 'print(\"Hello World!\")'",
                        "command_timeout": 60.0,
                    },
                },
            )
            command_operation_id = command_result.structured_content["async_operation_id"]
            command_status = await wait_for_mcp_operation(mcp, command_operation_id, timeout=120.0)
            bash_response = parse_forwarded_body(command_status)

            assert bash_response == {
                "stdout": "Hello World!",
                "stderr": "",
                "exit_code": 0,
            }

            finish_server_result = await mcp.call_tool(
                MCPToolName.FINISH_SERVER,
                {
                    "request": {
                        "client_id": client_id,
                        "namespace": DEFAULT_NAMESPACE,
                        "server_id": server_id,
                    },
                },
            )
            assert "available for reuse" in finish_server_result.structured_content["message"]
            active_server_id = None

            restart_result = await mcp.call_tool(
                MCPToolName.START_SERVER,
                {"request": start_request},
            )
            restart_operation_id = restart_result.structured_content["operation_id"]
            restart_status = await wait_for_mcp_operation(
                mcp,
                restart_operation_id,
                timeout=DEFAULT_SERVER_START_TIMEOUT,
                poll_interval=2.0,
            )
            restart_response = parse_operation_result(restart_status)
            assert restart_response["server_id"] == server_id
            active_server_id = server_id

        finally:
            if active_server_id is not None and client_id is not None:
                stop_server_result = await mcp.call_tool(
                    MCPToolName.STOP_SERVER,
                    {
                        "request": {
                            "client_id": client_id,
                            "namespace": DEFAULT_NAMESPACE,
                            "server_id": active_server_id,
                        },
                    },
                )
                operation_id = stop_server_result.structured_content.get("operation_id")
                if operation_id is not None:
                    await wait_for_mcp_operation(mcp, operation_id, timeout=120.0)

            if client_id is not None:
                stop_client_result = await mcp.call_tool(
                    MCPToolName.STOP_CLIENT,
                    {
                        "request": {
                            "client_id": client_id,
                            "namespace": DEFAULT_NAMESPACE,
                        },
                    },
                )
                operation_id = stop_client_result.structured_content.get("operation_id")
                if operation_id is not None:
                    await wait_for_mcp_operation(mcp, operation_id, timeout=120.0)
