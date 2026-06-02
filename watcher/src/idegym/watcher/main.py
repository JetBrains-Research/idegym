from asyncio import create_task, gather
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from idegym.api.config import Config
from idegym.api.health import HealthCheckResponse
from idegym.backend.utils.kubernetes_client import load_kubernetes_config
from idegym.backend.utils.logging import configure_logging
from idegym.orchestrator.database.database import connect_db_engine
from idegym.orchestrator.main import configure_process, load_config
from idegym.utils.logging import get_logger
from idegym.watcher.cleanup import cleanup_inactive_pods

logger = get_logger("idegym.watcher")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: Config = app.state.config

    await load_kubernetes_config()

    # The orchestrator owns the schema; the watcher only connects to an already-migrated database.
    connect_db_engine(
        db_url=config.orchestrator.database.url,
        config=config.orchestrator.sqlalchemy,
    )

    cleanup_task = create_task(
        name="idegym-inactive-pods-cleanup",
        coro=cleanup_inactive_pods(config.orchestrator.watcher),
    )
    logger.info("Started background task to cleanup inactive pods!")

    try:
        yield
    finally:
        logger.info("Stopping watcher cleanup task...")
        cleanup_task.cancel()
        await gather(cleanup_task, return_exceptions=True)
        logger.info("Watcher cleanup task stopped!")


def create_app() -> FastAPI:
    config = load_config()
    configure_process(config=config)

    app = FastAPI(title="IdeGYM Watcher", lifespan=lifespan)
    app.state.config = config

    @app.get("/health")
    async def health_check() -> HealthCheckResponse:
        return HealthCheckResponse(status="healthy")

    return app


def main() -> None:
    config = load_config()
    configure_logging(config=config.logging)

    uvicorn.run(
        app="idegym.watcher.main:create_app",
        factory=True,
        host=config.orchestrator.host,
        port=config.orchestrator.port,
        workers=1,
        log_config=None,
    )


if __name__ == "__main__":
    main()
