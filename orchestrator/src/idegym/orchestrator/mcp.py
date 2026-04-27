from collections.abc import Callable
from typing import Optional
from uuid import UUID

from fastmcp import FastMCP
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
from idegym.orchestrator.router.async_operation import get_operation_status as get_operation_status_endpoint
from idegym.orchestrator.router.build_images import build_and_push_with_config
from idegym.orchestrator.router.build_images import get_job_status_by_name as get_job_status_endpoint
from idegym.orchestrator.router.client import finish_client as finish_client_endpoint
from idegym.orchestrator.router.client import register_client_with_node_pool
from idegym.orchestrator.router.client import stop_client as stop_client_endpoint
from idegym.orchestrator.router.forwarding import forward_request_to_server
from idegym.orchestrator.router.server import finish_server as finish_server_endpoint
from idegym.orchestrator.router.server import restart_server as restart_server_endpoint
from idegym.orchestrator.router.server import start_server_with_config
from idegym.orchestrator.router.server import stop_server as stop_server_endpoint
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

    @mcp.tool
    async def register_client(request: RegisterClientRequest) -> RegisteredClientResponse:
        """Create a client record. If nodes_count is positive, pre-provision nodes asynchronously."""
        node_pool = _require_config(config).orchestrator.node_pool
        return await register_client_with_node_pool(request=request, node_pool=node_pool)

    @mcp.tool
    async def stop_client(request: StopClientRequest) -> StopClientResponse:
        """Tear down a client: stop alive servers, delete their Kubernetes resources, release nodes, and mark the client stopped."""
        return await stop_client_endpoint(request)

    @mcp.tool
    async def finish_client(request: FinishClientRequest) -> RegisteredClientResponse:
        """Mark a client and its alive servers as reusable without deleting Kubernetes resources."""
        return await finish_client_endpoint(request)

    @mcp.tool
    async def start_server(request: StartServerRequest) -> StartServerResponse:
        """Start a server pod from an OCI image or reuse a matching finished server."""
        return await start_server_with_config(request=request, config=_require_config(config))

    @mcp.tool
    async def stop_server(request: StopServerRequest) -> ServerActionResponse:
        """Stop a server and delete its Kubernetes resources."""
        return await stop_server_endpoint(request)

    @mcp.tool
    async def finish_server(request: FinishServerRequest) -> ServerActionResponse:
        """Mark a server as reusable without deleting its Kubernetes resources."""
        return await finish_server_endpoint(request)

    @mcp.tool
    async def restart_server(request: RestartServerRequest) -> ServerActionResponse:
        """Restart server pods and wait for them to become ready."""
        return await restart_server_endpoint(request)

    @mcp.tool
    async def build_images_from_yaml(request: BuildFromYamlRequest) -> BuildFromYamlResponse:
        """Start Kaniko image build jobs from IdeGYM image-builder YAML."""
        return await build_and_push_with_config(request=request, config=_require_config(config))

    @mcp.tool
    async def get_operation_status(request: GetOperationStatusRequest) -> AsyncOperationStatusResponse:
        """Look up the current status and result of a background operation."""
        return await get_operation_status_endpoint(request.operation_id)

    @mcp.tool
    async def get_job_status(request: GetJobStatusRequest) -> JobStatusResponse:
        """Look up the status and produced image tag for a Kaniko build job."""
        return await get_job_status_endpoint(request.job_name)

    @mcp.tool
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

    @mcp.tool
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

    return mcp
