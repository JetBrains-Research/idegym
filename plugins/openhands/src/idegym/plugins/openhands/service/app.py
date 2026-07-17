"""Compose the loopback FastAPI + FastMCP application."""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastmcp.utilities.lifespan import combine_lifespans
from idegym.plugins.openhands.api.errors import ServiceError
from idegym.plugins.openhands.api.names import INTERNAL_PREFIX
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.service import ToolRuntime
from idegym.plugins.openhands.service.mcp import build_mcp_server
from idegym.plugins.openhands.service.rest import build_rest_router


def build_app(config: Optional[RuntimeConfig] = None, runtime: Optional[ToolRuntime] = None) -> FastAPI:
    config = config or RuntimeConfig.from_env()
    runtime = runtime or ToolRuntime(config)
    # Build adapters + probe backends before wiring MCP so its tool list reflects the catalog.
    runtime.prepare()

    mcp_server = build_mcp_server(runtime)
    mcp_app = mcp_server.http_app(path="/")

    @asynccontextmanager
    async def _runtime_lifespan(_app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="IdeGYM OpenHands Tools Service")
    app.router.lifespan_context = combine_lifespans(_runtime_lifespan, mcp_app.lifespan)
    app.state.runtime = runtime
    app.include_router(build_rest_router(runtime), prefix=INTERNAL_PREFIX)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict:
        return {"live": True}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        health = runtime.health()
        return JSONResponse(status_code=200 if health.ready else 503, content=health.model_dump(mode="json"))

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    app.mount("/mcp", mcp_app)
    return app
