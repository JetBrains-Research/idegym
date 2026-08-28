from collections.abc import Callable
from typing import Any, Optional
from uuid import UUID

from fastmcp import Client, FastMCP
from httpx import AsyncClient
from idegym.api.config import Config
from idegym.api.orchestrator.build import BuildFromYamlRequest, BuildFromYamlResponse
from idegym.api.orchestrator.clients import (
    FinishClientRequest,
    RegisterClientRequest,
    RegisteredClientResponse,
    StopClientRequest,
    StopClientResponse,
)
from idegym.api.orchestrator.jobs import JobStatusResponse
from idegym.api.orchestrator.mcp import MCPToolName
from idegym.api.orchestrator.operations import AsyncOperationStatusResponse, ForwardRequestResponse
from idegym.api.orchestrator.servers import (
    FinishServerRequest,
    RestartServerRequest,
    ServerActionResponse,
    StartServerRequest,
    StartServerResponse,
    StopServerRequest,
)
from idegym.api.tools.bash import BashCommandRequest
from idegym.orchestrator.database.helpers import validate_server
from idegym.orchestrator.router.async_operation import get_operation_status as get_operation_status_endpoint
from idegym.orchestrator.router.build_images import build_and_push_with_config
from idegym.orchestrator.router.build_images import get_job_status_by_name as get_job_status_endpoint
from idegym.orchestrator.router.client import finish_client as finish_client_endpoint
from idegym.orchestrator.router.client import register_client_with_node_pool
from idegym.orchestrator.router.client import stop_client as stop_client_endpoint
from idegym.orchestrator.router.forwarding import build_server_host, forward_request_to_server
from idegym.orchestrator.router.server import finish_server as finish_server_endpoint
from idegym.orchestrator.router.server import restart_server_with_config, start_server_with_config
from idegym.orchestrator.router.server import stop_server_request as stop_server_endpoint
from pydantic import BaseModel, Field
from starlette.datastructures import Headers


class GetOperationStatusRequest(BaseModel):
    operation_id: int = Field(description="Async operation ID returned by an orchestrator tool")


class GetJobStatusRequest(BaseModel):
    job_name: str = Field(description="Kaniko build job name returned by build_images_from_yaml")


class ForwardServerRequest(BaseModel):
    client_id: UUID = Field(description="UUID of the client that owns the server")
    server_id: int = Field(description="Numeric IdeGYM server ID to forward the request to")
    path: str = Field(description='Path on the server, for example "api/tools/bash"')
    method: str = Field(default="GET", description="HTTP method to use for the forwarded request")
    headers: Optional[dict[str, str]] = Field(default=None, description="HTTP headers to forward")
    body: str = Field(default="", description="Request body to forward as text")


class RunBashCommandToolRequest(BaseModel):
    client_id: UUID = Field(description="UUID of the client that owns the server")
    server_id: int = Field(description="Numeric IdeGYM server ID to run the command on")
    command: str = Field(description="Command to execute as a bash script")
    command_timeout: float = Field(default=600.0, description="Timeout for command execution in seconds")
    graceful_termination_timeout: float = Field(
        default=2.0,
        description="Timeout in seconds for graceful process termination",
    )


class ListServerMcpToolsRequest(BaseModel):
    client_id: UUID = Field(description="UUID of the client that owns the server")
    server_id: int = Field(description="Numeric IdeGYM server ID")


class McpToolInfo(BaseModel):
    name: str = Field(description="Tool name")
    description: Optional[str] = Field(default=None, description="Tool description")
    input_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema describing the tool's input arguments",
    )


class ListServerMcpToolsResponse(BaseModel):
    tools: list[McpToolInfo] = Field(description="MCP tools available on the server")


class CallServerMcpToolRequest(BaseModel):
    client_id: UUID = Field(description="UUID of the client that owns the server")
    server_id: int = Field(description="Numeric IdeGYM server ID")
    tool_name: str = Field(description="Tool name as returned by list_server_mcp_tools")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Arguments to pass to the tool")


class CallServerMcpToolResponse(BaseModel):
    content: list[dict[str, Any]] = Field(description="MCP tool result content items (text, images, etc.)")
    is_error: bool = Field(default=False, description="True if the tool call resulted in an error")


def _require_config(config: Optional[Config]) -> Config:
    if config is None:
        raise RuntimeError("This MCP tool requires orchestrator configuration")
    return config


def _require_http_client(get_http_client: Optional[Callable[[], AsyncClient]]) -> AsyncClient:
    if get_http_client is None:
        raise RuntimeError("This MCP tool requires the orchestrator HTTP client")
    return get_http_client()


def create_mcp_server(
    config: Optional[Config] = None,
    get_http_client: Optional[Callable[[], AsyncClient]] = None,
) -> FastMCP:
    mcp = FastMCP("IdeGYM Orchestrator")

    @mcp.tool(name=MCPToolName.REGISTER_CLIENT)
    async def register_client(request: RegisterClientRequest) -> RegisteredClientResponse:
        """Create a client record. If nodes_count is positive, pre-provision nodes asynchronously."""
        orchestrator = _require_config(config).orchestrator
        return await register_client_with_node_pool(
            request=request, node_pool=orchestrator.node_pool, scheduling=orchestrator.scheduling
        )

    @mcp.tool(name=MCPToolName.STOP_CLIENT)
    async def stop_client(request: StopClientRequest) -> StopClientResponse:
        """Tear down a client: stop alive servers, delete their Kubernetes resources, release nodes, and mark the client stopped."""
        return await stop_client_endpoint(request)

    @mcp.tool(name=MCPToolName.FINISH_CLIENT)
    async def finish_client(request: FinishClientRequest) -> RegisteredClientResponse:
        """Mark a client and its alive servers as reusable without deleting Kubernetes resources."""
        return await finish_client_endpoint(request)

    @mcp.tool(name=MCPToolName.START_SERVER)
    async def start_server(request: StartServerRequest) -> StartServerResponse:
        """Start a server pod from an OCI image or reuse a matching finished server."""
        return await start_server_with_config(request=request, config=_require_config(config))

    @mcp.tool(name=MCPToolName.STOP_SERVER)
    async def stop_server(request: StopServerRequest) -> ServerActionResponse:
        """Stop a server and delete its Kubernetes resources."""
        return await stop_server_endpoint(request)

    @mcp.tool(name=MCPToolName.FINISH_SERVER)
    async def finish_server(request: FinishServerRequest) -> ServerActionResponse:
        """Mark a server as reusable without deleting its Kubernetes resources."""
        return await finish_server_endpoint(request)

    @mcp.tool(name=MCPToolName.RESTART_SERVER)
    async def restart_server(request: RestartServerRequest) -> ServerActionResponse:
        """Restart server pods and wait for them to become ready."""
        return await restart_server_with_config(request=request, config=_require_config(config))

    @mcp.tool(name=MCPToolName.BUILD_IMAGES_FROM_YAML)
    async def build_images_from_yaml(request: BuildFromYamlRequest) -> BuildFromYamlResponse:
        """Start Kaniko image build jobs from IdeGYM image-builder YAML."""
        return await build_and_push_with_config(request=request, config=_require_config(config))

    @mcp.tool(name=MCPToolName.GET_OPERATION_STATUS)
    async def get_operation_status(request: GetOperationStatusRequest) -> AsyncOperationStatusResponse:
        """Look up the current status and result of a background operation."""
        return await get_operation_status_endpoint(request.operation_id)

    @mcp.tool(name=MCPToolName.GET_JOB_STATUS)
    async def get_job_status(request: GetJobStatusRequest) -> JobStatusResponse:
        """Look up the status and produced image tag for a Kaniko build job."""
        return await get_job_status_endpoint(request.job_name)

    @mcp.tool(name=MCPToolName.FORWARD_REQUEST)
    async def forward_request(request: ForwardServerRequest) -> ForwardRequestResponse:
        """Forward an HTTP request to a running IdeGYM server."""
        return await forward_request_to_server(
            client_id=request.client_id,
            server_id=request.server_id,
            path=request.path,
            method=request.method,
            headers=Headers(headers=request.headers or {}),
            body=request.body,
            http_client=_require_http_client(get_http_client),
        )

    @mcp.tool(name=MCPToolName.RUN_BASH_COMMAND)
    async def run_bash_command(request: RunBashCommandToolRequest) -> ForwardRequestResponse:
        """Execute a bash script on a running IdeGYM server."""
        bash_request = BashCommandRequest(
            command=request.command,
            timeout=request.command_timeout,
            graceful_termination_timeout=request.graceful_termination_timeout,
        )
        return await forward_request_to_server(
            client_id=request.client_id,
            server_id=request.server_id,
            path="api/tools/bash",
            method="POST",
            headers=Headers(headers={"Content-Type": "application/json"}),
            body=bash_request.model_dump_json(),
            http_client=_require_http_client(get_http_client),
        )

    @mcp.tool(name=MCPToolName.LIST_SERVER_MCP_TOOLS)
    async def list_server_mcp_tools(request: ListServerMcpToolsRequest) -> ListServerMcpToolsResponse:
        """List all MCP tools exposed by a running IdeGYM server."""
        server = await validate_server(client_id=request.client_id, server_id=request.server_id)
        host = build_server_host(server.generated_name, server.namespace)
        url = f"http://{host}:{server.service_port}/mcp"
        async with Client(url) as client:
            tools = await client.list_tools()
        return ListServerMcpToolsResponse(
            tools=[
                McpToolInfo(
                    name=t.name,
                    description=t.description,
                    input_schema=t.inputSchema,
                )
                for t in tools
            ]
        )

    @mcp.tool(name=MCPToolName.CALL_SERVER_MCP_TOOL)
    async def call_server_mcp_tool(request: CallServerMcpToolRequest) -> CallServerMcpToolResponse:
        """Call an MCP tool on a running IdeGYM server by name."""
        server = await validate_server(client_id=request.client_id, server_id=request.server_id)
        host = build_server_host(server.generated_name, server.namespace)
        url = f"http://{host}:{server.service_port}/mcp"
        async with Client(url) as client:
            result = await client.call_tool(request.tool_name, request.arguments)
        return CallServerMcpToolResponse(
            content=[c.model_dump(mode="json") for c in result.content],
            is_error=result.is_error,
        )

    return mcp
