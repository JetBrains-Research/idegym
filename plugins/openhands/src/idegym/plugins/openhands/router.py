"""Public IdeGYM REST router that proxies ``/api/openhands/...`` to the loopback service.

Per-tool routes are generated from the same static catalog manifest the service uses, so each tool
gets its own operation in IdeGYM's OpenAPI without any network call at import time. The
router is a typed proxy — it never instantiates a second ToolRuntime.
"""

from typing import Any, Optional

from fastapi import APIRouter
from idegym.plugins.openhands.api.models import (
    TerminalCreateRequest,
    TerminalExecuteRequest,
    TerminalInputRequest,
    TerminalPollRequest,
    ToolActionRequest,
    ToolCallRequest,
)
from idegym.plugins.openhands.api.names import PUBLIC_PREFIX
from idegym.plugins.openhands.proxy import LoopbackProxy
from idegym.plugins.openhands.runtime.catalog import ToolCatalog
from idegym.plugins.openhands.runtime.config import RuntimeConfig


def build_openhands_router(proxy: Optional[LoopbackProxy] = None) -> APIRouter:
    proxy = proxy or LoopbackProxy()
    config = RuntimeConfig.from_env()
    catalog = ToolCatalog(config.profile, config.enabled_tools, config.disabled_tools)

    router = APIRouter(prefix=PUBLIC_PREFIX, tags=["openhands"])

    # -- discovery / health ------------------------------------------------

    @router.get("/health")
    async def health() -> Any:
        return await proxy.forward("GET", "/health")

    @router.get("/capabilities")
    async def capabilities() -> Any:
        return await proxy.forward("GET", "/capabilities")

    @router.get("/tools")
    async def tools() -> Any:
        return await proxy.forward("GET", "/tools")

    @router.get("/tools/{tool_name}/schema")
    async def tool_schema(tool_name: str) -> Any:
        return await proxy.forward("GET", f"/tools/{tool_name}/schema")

    @router.get("/artifacts/{artifact_id}")
    async def artifact(artifact_id: str) -> Any:
        # Stream the download instead of buffering the whole artifact in the proxy.
        return await proxy.stream("GET", f"/artifacts/{artifact_id}")

    # -- tool call (generic + per-tool) ------------------------------------

    @router.post("/call")
    async def call(request: ToolCallRequest) -> Any:
        return await proxy.forward("POST", "/call", json_body=request.model_dump(mode="json"))

    def _make_tool_route(tool_name: str):
        async def _route(request: ToolActionRequest) -> Any:
            return await proxy.forward("POST", f"/tools/{tool_name}", json_body=request.model_dump(mode="json"))

        return _route

    for entry in catalog.route_entries():
        router.add_api_route(
            f"/tools/{entry.name}",
            _make_tool_route(entry.name),
            methods=["POST"],
            name=f"call_{entry.name}",
            operation_id=f"openhands_call_{entry.name}",
            summary=f"Invoke the {entry.name} tool",
        )

    # -- terminal lifecycle ------------------------------------------------

    @router.post("/terminals")
    async def create_terminal(request: TerminalCreateRequest) -> Any:
        return await proxy.forward("POST", "/terminals", json_body=request.model_dump(mode="json"))

    @router.get("/terminals")
    async def list_terminals() -> Any:
        return await proxy.forward("GET", "/terminals")

    @router.post("/terminals/reset-all")
    async def reset_all_terminals() -> Any:
        return await proxy.forward("POST", "/terminals/reset-all")

    @router.get("/terminals/{terminal_id}")
    async def get_terminal(terminal_id: str) -> Any:
        return await proxy.forward("GET", f"/terminals/{terminal_id}")

    @router.post("/terminals/{terminal_id}/execute")
    async def execute_terminal(terminal_id: str, request: TerminalExecuteRequest) -> Any:
        return await proxy.forward(
            "POST", f"/terminals/{terminal_id}/execute", json_body=request.model_dump(mode="json")
        )

    @router.post("/terminals/{terminal_id}/input")
    async def input_terminal(terminal_id: str, request: TerminalInputRequest) -> Any:
        return await proxy.forward("POST", f"/terminals/{terminal_id}/input", json_body=request.model_dump(mode="json"))

    @router.post("/terminals/{terminal_id}/poll")
    async def poll_terminal(terminal_id: str, request: TerminalPollRequest) -> Any:
        return await proxy.forward("POST", f"/terminals/{terminal_id}/poll", json_body=request.model_dump(mode="json"))

    @router.post("/terminals/{terminal_id}/interrupt")
    async def interrupt_terminal(terminal_id: str) -> Any:
        return await proxy.forward("POST", f"/terminals/{terminal_id}/interrupt")

    @router.post("/terminals/{terminal_id}/reset")
    async def reset_terminal(terminal_id: str) -> Any:
        return await proxy.forward("POST", f"/terminals/{terminal_id}/reset")

    @router.get("/terminals/{terminal_id}/capture")
    async def capture_terminal(terminal_id: str) -> Any:
        return await proxy.forward("GET", f"/terminals/{terminal_id}/capture")

    @router.delete("/terminals/{terminal_id}")
    async def close_terminal(terminal_id: str) -> Any:
        return await proxy.forward("DELETE", f"/terminals/{terminal_id}")

    # -- environment reset -------------------------------------------------

    @router.post("/reset")
    async def reset(reason: str = "manual") -> Any:
        return await proxy.forward("POST", "/reset", params={"reason": reason})

    return router
