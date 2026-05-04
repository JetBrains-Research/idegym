from asyncio import sleep
from os import getpid, kill
from signal import SIGTERM
from typing import Annotated

from anyio import open_file as open
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, status
from fastapi.background import BackgroundTasks
from fastapi.requests import Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from idegym.api.capabilities import CapabilitiesResponse
from idegym.api.data import DataSize
from idegym.api.paths import ActuatorPath
from idegym.api.type import Duration
from idegym.utils.logging import get_logger
from prometheus_client import REGISTRY
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST, generate_latest

from server.dependencies import Container

logger = get_logger(__name__)

router = APIRouter()


@router.get("")
async def root(request: Request):
    url = str(request.url) + router.url_path_for("health")
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get(ActuatorPath.HEALTH)
async def health():
    return Response()


@router.get(ActuatorPath.CAPABILITIES)
async def capabilities():
    from server.main import _loaded_plugins

    return CapabilitiesResponse(plugins=_loaded_plugins)


@router.get(ActuatorPath.LOG)
@inject
async def log(
    path: Annotated[str, Depends(Provide[Container.config.logging.file_path])],
    size: Annotated[DataSize, Depends(Provide[Container.config.server.response_buffer_size])],
):
    async def reader():
        async with await open(path, "rb") as file:
            while chunk := await file.read(size.bytes):
                yield chunk

    return StreamingResponse(content=reader(), media_type="application/octet-stream")


@router.get(ActuatorPath.METRICS)
async def metrics():
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )


@router.post(ActuatorPath.SHUTDOWN)
@inject
async def shutdown(
    tasks: BackgroundTasks,
    delay: Annotated[Duration, Depends(Provide[Container.config.server.shutdown_delay])],
):
    async def terminate():
        await sleep(delay.seconds)
        kill(getpid(), SIGTERM)

    tasks.add_task(terminate)
    return Response(status_code=status.HTTP_202_ACCEPTED)
