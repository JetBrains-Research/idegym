from asyncio import create_task, gather, get_event_loop, sleep
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from httpx import AsyncClient, Limits, Timeout
from hydra import compose, initialize_config_dir
from idegym.api.config import Config
from idegym.backend.utils.diagnostics import dump_tasks_periodically
from idegym.backend.utils.instrumentation.uvicorn import UvicornInstrumentor
from idegym.backend.utils.kubernetes_client import load_kubernetes_config
from idegym.backend.utils.logging import configure_logging, configure_sqlalchemy_logging
from idegym.backend.utils.otel import configure_telemetry, system_metrics_config
from idegym.backend.utils.starlette.middleware import AsyncioTaskContextMiddleware, TracingMiddleware
from idegym.orchestrator.database.database import init_db
from idegym.orchestrator.router import async_operation, build_images, client, dashboard, diagnostics, forwarding, server
from idegym.orchestrator.watcher import cleanup_inactive_pods
from idegym.utils import __version__
from idegym.utils.logging import get_logger
from omegaconf import OmegaConf
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.jinja2 import Jinja2Instrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor

logger = get_logger("idegym.orchestrator")


def load_config() -> Config:
    config_dir = Path(__file__).resolve().parents[3] / "hydra_configs"
    with initialize_config_dir(version_base=None, config_dir=str(config_dir)):
        cfg = compose(config_name="config")
    container: dict[str, Any] = OmegaConf.to_container(cfg=cfg, resolve=True)
    return Config(**container)


def configure_process(config: Config) -> None:
    configure_logging(config=config.logging)
    configure_sqlalchemy_logging(config=config.logging)
    configure_telemetry(config=config.otel)
    logger.info(f"Version: {__version__}")


def prepare_prometheus_multiprocess_dir(config: Config) -> None:
    multiprocess_dir = config.orchestrator.prometheus_multiproc_dir
    if not multiprocess_dir:
        return

    multiprocess_path = Path(multiprocess_dir)
    multiprocess_path.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: Config = app.state.config

    # Load Kubernetes config
    await load_kubernetes_config()

    # Initialize database
    await init_db(
        db_url=config.orchestrator.database.url,
        config=config.orchestrator.sqlalchemy,
        clean_database=config.orchestrator.database.clean_database,
    )

    # Schedule cleaning task first, as it should not depend on the client...
    cleanup_task = create_task(
        name="idegym-inactive-pods-cleanup",
        coro=cleanup_inactive_pods(config.orchestrator.watcher),
    )
    logger.info("Started background task to cleanup inactive pods!")

    # Start a background task to dump asyncio coroutines periodically...
    get_event_loop().set_debug(config.orchestrator.asyncio.debug)
    coroutine_dump_task = create_task(
        name="idegym-coroutine-dump",
        coro=(
            dump_tasks_periodically(config.orchestrator.asyncio.dump_interval)
            if config.orchestrator.asyncio.debug
            else sleep(0)
        ),
    )

    # Create the HTTP client in an `async` context manager and `yield` the `lifespan`...
    async with AsyncClient(
        timeout=Timeout(
            timeout=config.orchestrator.client_request_timeout,
            read=config.orchestrator.client_request_timeout,
            write=10.0,
            connect=10.0,
        ),
        limits=Limits(
            max_connections=config.orchestrator.connection_limits.max_connections_or_asyncio_tasks,
            max_keepalive_connections=config.orchestrator.connection_limits.max_keepalive_connections,
            keepalive_expiry=config.orchestrator.connection_limits.keepalive_expiry,
        ),
    ) as http_client:
        logger.info("HTTP client initialized!")
        app.state.http_client = http_client
        yield
        logger.info("Closing HTTP client...")
    logger.info("HTTP client closed!")

    # Cancel the background tasks
    cleanup_task.cancel()
    coroutine_dump_task.cancel()
    await gather(cleanup_task, coroutine_dump_task)


def create_app() -> FastAPI:
    config = load_config()
    configure_process(config=config)

    app = FastAPI(title="IdeGYM Orchestrator", lifespan=lifespan)
    app.add_middleware(TracingMiddleware)
    app.add_middleware(AsyncioTaskContextMiddleware)
    app.include_router(diagnostics.router)
    app.include_router(client.router)
    app.include_router(server.router)
    app.include_router(build_images.router)
    app.include_router(forwarding.router)
    app.include_router(async_operation.router)
    app.include_router(dashboard.router)

    AsyncioInstrumentor().instrument()
    Jinja2Instrumentor().instrument()
    SystemMetricsInstrumentor(config=system_metrics_config).instrument()
    AioHttpClientInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    FastAPIInstrumentor().instrument_app(app)
    UvicornInstrumentor().instrument()

    app.state.config = config

    return app


def main() -> None:
    config = load_config()
    prepare_prometheus_multiprocess_dir(config)

    uvicorn.run(
        app="idegym.orchestrator.main:create_app",
        factory=True,
        host=config.orchestrator.host,
        port=config.orchestrator.port,
        workers=config.orchestrator.workers,
        log_config=None,
        limit_concurrency=config.orchestrator.connection_limits.max_connections_or_asyncio_tasks,
        backlog=4096,
    )


if __name__ == "__main__":
    main()
