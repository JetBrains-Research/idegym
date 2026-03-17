from fastapi import APIRouter, Request, Response
from idegym.api.config import Config
from idegym.api.health import HealthCheckResponse
from idegym.utils.logging import get_logger
from prometheus_client import CollectorRegistry, multiprocess
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter()
logger = get_logger(__name__)


def generate_metrics(config: Config):
    multiprocess_dir = config.orchestrator.prometheus_multiproc_dir
    registry = CollectorRegistry(auto_describe=True)
    multiprocess.MultiProcessCollector(registry, path=multiprocess_dir)
    return generate_latest(registry)


@router.get("/health")
async def health_check(request: Request):
    return HealthCheckResponse(status="healthy")


@router.get("/metrics")
async def metrics(request: Request):
    config: Config = request.app.state.config
    return Response(
        content=generate_metrics(config),
        media_type=CONTENT_TYPE_LATEST,
    )
