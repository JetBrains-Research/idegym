from contextlib import asynccontextmanager
from os.path import abspath, dirname, join
from pathlib import Path

from fastapi import FastAPI, status
from fastapi.requests import Request
from fastapi.responses import Response
from hydra import main as hydra
from idegym.api.config import Config
from idegym.api.paths import API_BASE_PATH
from idegym.backend.utils.bash_executor import BashCommandExecutionTimeoutError
from idegym.backend.utils.instrumentation.uvicorn import UvicornInstrumentor
from idegym.backend.utils.logging import configure_logging, create_uvicorn_logging_config
from idegym.backend.utils.otel import configure_telemetry, system_metrics_config
from idegym.backend.utils.starlette.middleware import (
    AsyncioTaskContextMiddleware,
    ShutdownMiddleware,
    TracingMiddleware,
)
from idegym.backend.utils.starlette.responses import ErrorResponse
from idegym.utils import __version__
from idegym.utils.logging import get_logger
from omegaconf import DictConfig, OmegaConf
from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from uvicorn import Config as UvicornConfig
from uvicorn import Server as UvicornServer

from server.dependencies import Container
from server.router import fs, project, rewards, root, tools

logger = get_logger("idegym.server")


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.container.init_resources()
    yield
    application.container.shutdown_resources()


app = FastAPI(title="IdeGYM Server", lifespan=lifespan)
app.container = Container()
app.add_middleware(ShutdownMiddleware)
app.add_middleware(TracingMiddleware)
app.add_middleware(AsyncioTaskContextMiddleware)
app.container.wire(packages=[fs, project, rewards, root, tools])
app.include_router(prefix=API_BASE_PATH, router=root.router)
app.include_router(prefix=API_BASE_PATH, router=project.router)
app.include_router(prefix=API_BASE_PATH, router=rewards.router)
app.include_router(prefix=API_BASE_PATH, router=tools.router)
app.include_router(prefix=API_BASE_PATH, router=fs.router)

AsyncioInstrumentor().instrument()
SystemMetricsInstrumentor(config=system_metrics_config).instrument()
HTTPXClientInstrumentor().instrument()
FastAPIInstrumentor().instrument_app(app)
UvicornInstrumentor().instrument()


# TODO: Load traceback inclusion from an environment variable


@app.exception_handler(OSError)
async def os_error(_request: Request, ex: OSError):
    return ErrorResponse(exception=ex, status_code=status.HTTP_400_BAD_REQUEST)


@app.exception_handler(FileNotFoundError)
async def file_not_found_error(_request: Request, ex: FileNotFoundError):
    return ErrorResponse(exception=ex, status_code=status.HTTP_404_NOT_FOUND)


@app.exception_handler(PermissionError)
async def permission_error(_request: Request, ex: PermissionError):
    return ErrorResponse(exception=ex, status_code=status.HTTP_403_FORBIDDEN)


@app.exception_handler(BashCommandExecutionTimeoutError)
async def bash_command_timed_out(_request: Request, ex: BashCommandExecutionTimeoutError):
    return ErrorResponse(exception=ex, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@app.exception_handler(status.HTTP_404_NOT_FOUND)
async def not_found(_request: Request, _call_next):
    return Response(status_code=status.HTTP_404_NOT_FOUND)


@app.exception_handler(Exception)
async def exception(_request: Request, ex: Exception):
    return ErrorResponse(exception=ex)


@hydra(
    version_base=None,
    config_path=join(dirname(abspath(__file__)), "hydra_configs"),
    config_name="config",
)
def main(cfg: DictConfig):
    container = OmegaConf.to_container(cfg=cfg, resolve=True)
    config = Config(**container)
    options = config.model_dump()
    app.container.config.from_dict(options=options)
    configure_logging(config=config.logging)
    configure_telemetry(config=config.otel)
    Path(config.project.path).mkdir(parents=True, exist_ok=True)
    logger.info(f"Version: {__version__}")

    server = UvicornServer(
        config=UvicornConfig(
            app=app,
            host=config.orchestrator.host,
            port=config.orchestrator.port,
            log_config=create_uvicorn_logging_config(
                config=config.logging,
            ),
        ),
    )

    app.state.server = server

    server.run()


if __name__ == "__main__":
    main()
