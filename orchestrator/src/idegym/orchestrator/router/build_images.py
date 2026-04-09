import tempfile
from os import environ as env
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from idegym.api.orchestrator.build import BuildFromYamlRequest, BuildFromYamlResponse
from idegym.api.orchestrator.jobs import JobStatusResponse
from idegym.orchestrator.database.helpers import find_kaniko_job_status
from idegym.orchestrator.kaniko_docker_api import IdeGYMKanikoDockerAPI
from idegym.orchestrator.util.decorators import handle_general_exceptions
from idegym.utils.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/api/build-push-images")
@handle_general_exceptions(error_message="Failed to start image building jobs from YAML")
async def build_and_push(request: BuildFromYamlRequest):
    """Build Docker images from a YAML file using Kaniko in Kubernetes."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as yaml_file:
        yaml_file.write(request.yaml_content)
        yaml_path = yaml_file.name

    try:
        # Check if insecure registry should be used (e.g., for local Minikube registry)
        insecure_registry = env.get("KANIKO_INSECURE_REGISTRY", "false").lower() == "true"
        kaniko_api = IdeGYMKanikoDockerAPI(namespace=request.namespace, insecure_registry=insecure_registry)
        job_names = await kaniko_api.build_and_push_images(path=Path(yaml_path))

        logger.info(f"Successfully started: {job_names}")
        return BuildFromYamlResponse(job_names=job_names)
    finally:
        Path(yaml_path).unlink(missing_ok=True)


@router.get("/api/jobs/status/{job_name}")
@handle_general_exceptions(error_message="Failed to get kaniko job status")
async def get_job_status_by_name(job_name: str):
    """Get job status and tag by job name."""
    job_status = await find_kaniko_job_status(job_name=job_name)
    if not job_status:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Kaniko job with name {job_name} not found")
    return JobStatusResponse.model_validate(job_status, from_attributes=True)
