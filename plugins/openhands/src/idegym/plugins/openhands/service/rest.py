"""Internal REST API (``/v1``) projecting the ToolRuntime.

Per-tool routes are generated from the static catalog manifest so each enabled tool has its own
stable path + operation id in OpenAPI. All routes dispatch into the shared
runtime; none owns an executor or terminal.
"""

from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from idegym.plugins.openhands.api.errors import ServiceError
from idegym.plugins.openhands.api.models import (
    CapabilityResponse,
    HealthResponse,
    ResetResponse,
    TerminalCreateRequest,
    TerminalDescriptor,
    TerminalExecuteRequest,
    TerminalInputRequest,
    TerminalPollRequest,
    TerminalResult,
    ToolActionRequest,
    ToolCallRequest,
    ToolCallResult,
    ToolDescriptor,
    ToolSchemaResponse,
)
from idegym.plugins.openhands.runtime.service import ToolRuntime


def _error_response(exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_response().model_dump(mode="json"))


def build_rest_router(runtime: ToolRuntime) -> APIRouter:
    router = APIRouter()

    # -- discovery / health ------------------------------------------------

    @router.get("/health", response_model=HealthResponse, tags=["discovery"])
    async def health() -> HealthResponse:
        return runtime.health()

    @router.get("/capabilities", response_model=CapabilityResponse, tags=["discovery"])
    async def capabilities() -> CapabilityResponse:
        return runtime.capability_response()

    @router.get("/tools", response_model=list[ToolDescriptor], tags=["discovery"])
    async def tools() -> list[ToolDescriptor]:
        return runtime.list_tools()

    @router.get("/tools/{tool_name}/schema", response_model=ToolSchemaResponse, tags=["discovery"])
    async def tool_schema(tool_name: str) -> Any:
        try:
            return runtime.get_tool_schema(tool_name)
        except ServiceError as exc:
            return _error_response(exc)

    @router.get("/artifacts/{artifact_id}", tags=["artifacts"])
    async def get_artifact(artifact_id: str) -> Any:
        try:
            data, descriptor = runtime.artifacts.read(artifact_id)
        except ServiceError as exc:
            return _error_response(exc)
        return Response(content=data, media_type=descriptor.media_type)

    # -- generic + per-tool call routes -----------------

    @router.post("/call", response_model=ToolCallResult, tags=["tools"])
    async def call(request: ToolCallRequest) -> Any:
        try:
            return await runtime.call_tool(
                request.tool,
                request.arguments,
                terminal_id=request.context.terminal_id,
                request_id=request.context.request_id,
            )
        except ServiceError as exc:
            return _error_response(exc)

    def _make_tool_route(tool_name: str):
        async def _route(request: ToolActionRequest) -> Any:
            try:
                return await runtime.call_tool(
                    tool_name,
                    request.arguments,
                    terminal_id=request.context.terminal_id,
                    request_id=request.context.request_id,
                )
            except ServiceError as exc:
                return _error_response(exc)

        return _route

    for entry in runtime.catalog.route_entries():
        router.add_api_route(
            f"/tools/{entry.name}",
            _make_tool_route(entry.name),
            methods=["POST"],
            response_model=ToolCallResult,
            name=f"call_{entry.name}",
            operation_id=f"call_{entry.name}",
            tags=["tools"],
            summary=f"Invoke the {entry.name} tool",
        )

    # -- terminal lifecycle -----------------------------

    @router.post("/terminals", response_model=TerminalDescriptor, tags=["terminals"])
    async def create_terminal(request: TerminalCreateRequest) -> Any:
        try:
            return await runtime.terminals.create(request)
        except ServiceError as exc:
            return _error_response(exc)

    @router.get("/terminals", response_model=list[TerminalDescriptor], tags=["terminals"])
    async def list_terminals() -> list[TerminalDescriptor]:
        return runtime.terminals.list()

    @router.get("/terminals/{terminal_id}", response_model=TerminalDescriptor, tags=["terminals"])
    async def get_terminal(terminal_id: str) -> Any:
        try:
            return runtime.terminals.get(terminal_id)
        except ServiceError as exc:
            return _error_response(exc)

    @router.post("/terminals/{terminal_id}/execute", response_model=TerminalResult, tags=["terminals"])
    async def terminal_execute(terminal_id: str, request: TerminalExecuteRequest) -> Any:
        try:
            return await runtime.terminals.execute(
                terminal_id,
                request.command,
                timeout=request.timeout,
                is_input=request.is_input,
                reset=request.reset,
                call_id="",
            )
        except ServiceError as exc:
            return _error_response(exc)

    @router.post("/terminals/{terminal_id}/input", response_model=TerminalResult, tags=["terminals"])
    async def terminal_input(terminal_id: str, request: TerminalInputRequest) -> Any:
        try:
            return await runtime.terminals.input(terminal_id, request.text, timeout=request.timeout)
        except ServiceError as exc:
            return _error_response(exc)

    @router.post("/terminals/{terminal_id}/poll", response_model=TerminalResult, tags=["terminals"])
    async def terminal_poll(terminal_id: str, request: TerminalPollRequest) -> Any:
        try:
            return await runtime.terminals.poll(terminal_id, timeout=request.timeout)
        except ServiceError as exc:
            return _error_response(exc)

    @router.post("/terminals/{terminal_id}/interrupt", response_model=TerminalResult, tags=["terminals"])
    async def terminal_interrupt(terminal_id: str) -> Any:
        try:
            return await runtime.terminals.interrupt(terminal_id)
        except ServiceError as exc:
            return _error_response(exc)

    @router.post("/terminals/{terminal_id}/reset", response_model=TerminalDescriptor, tags=["terminals"])
    async def terminal_reset(terminal_id: str) -> Any:
        try:
            return await runtime.terminals.reset(terminal_id)
        except ServiceError as exc:
            return _error_response(exc)

    @router.get("/terminals/{terminal_id}/capture", tags=["terminals"])
    async def terminal_capture(terminal_id: str) -> Any:
        try:
            return {"terminal_id": terminal_id, "content": await runtime.terminals.capture(terminal_id)}
        except ServiceError as exc:
            return _error_response(exc)

    @router.delete("/terminals/{terminal_id}", tags=["terminals"])
    async def terminal_close(terminal_id: str) -> Any:
        await runtime.terminals.close(terminal_id)
        return {"closed": terminal_id}

    @router.post("/terminals/reset-all", tags=["terminals"])
    async def terminals_reset_all() -> Any:
        count = await runtime.terminals.reset_all()
        return {"terminated": count}

    # -- environment reset -----------------------------

    @router.post("/reset", response_model=ResetResponse, tags=["reset"])
    async def reset(reason: str = "manual") -> ResetResponse:
        return await runtime.reset_environment(reason)

    return router
