from asyncio import create_task, sleep, timeout
from os import environ as env
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from idegym.api.docker import BaseImage
from idegym.api.download import DownloadRequest
from idegym.api.image_build import parse_image_build_pipeline
from idegym.api.status import Status
from idegym.backend.utils.kubernetes_client import build_and_push_image_with_kaniko, clean_up_after_job, get_job_status
from idegym.orchestrator.database.database import get_db_session, save_job_status, update_job_status
from idegym.utils import __version__
from idegym.utils.logging import get_logger
from idegym.utils.path import get_base_filename

logger = get_logger(__name__)

__DOCKER_REPOSITORY__ = "ghcr.io/jetbrains-research/idegym"


class IdeGYMKanikoDockerAPI:
    def __init__(
        self,
        namespace: str = "idegym",
        job_timeout: float = 2400,
    ):
        self._namespace = namespace
        self._job_timeout = job_timeout

    async def build_and_push_single_image(
        self,
        request: DownloadRequest,
        image_version: str,
        dockerfile_content: str,
        labels: Dict[str, str],
        base: Optional[BaseImage] = None,
        runtime_class_name: str = "gvisor",
        pod_resources: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> str:
        logger.info(f"Download request: {request.descriptor.url}, {request.descriptor.name}")

        registry = __DOCKER_REPOSITORY__
        image_name = get_base_filename(request.descriptor.name)
        tag = f"{registry}/{image_name}:{image_version}"
        idegym_version = env.get("IDEGYM_VERSION") or __version__

        job_name = await build_and_push_image_with_kaniko(
            request=request,
            tag=tag,
            base=base.value if base is not None else None,
            service_version=idegym_version,
            dockerfile_content=dockerfile_content,
            labels=labels,
            namespace=self._namespace,
            ttl_seconds_after_finished=300,
            runtime_class_name=runtime_class_name,
            resources=pod_resources,
        )

        create_task(self.monitor_image_building_job(job_name, tag, request_id))

        return job_name

    async def monitor_image_building_job(self, job_name: str, tag: str, request_id: Optional[str] = None) -> None:
        try:
            async with get_db_session() as db:
                await save_job_status(db, job_name, status=Status.IN_PROGRESS, tag=tag, request_id=request_id)

            try:
                async with timeout(self._job_timeout):
                    status = await get_job_status(job_name, self._namespace)
                    while status == Status.IN_PROGRESS:
                        await sleep(2)
                        status = await get_job_status(job_name, self._namespace)

                    async with get_db_session() as db:
                        await update_job_status(db, job_name, status=status, tag=tag, request_id=request_id)

                    if status == Status.SUCCESS:
                        logger.info(f"Job '{job_name}' finished successfully. Request ID: {request_id}")
                    else:
                        logger.error(
                            f"Job '{job_name}' was terminated with status '{status}'. Request ID: {request_id}"
                        )
            except TimeoutError:
                logger.error(
                    f"Job '{job_name}' monitoring timed out after {self._job_timeout}s. Request ID: {request_id}"
                )
                async with get_db_session() as db:
                    await update_job_status(db, job_name, status=Status.FAILURE, tag=tag, request_id=request_id)

            await clean_up_after_job(job_name, self._namespace)
        except Exception:
            logger.exception(f"Error monitoring job '{job_name}'. Request ID: {request_id}")
            try:
                async with get_db_session() as db:
                    await update_job_status(db, job_name, status=Status.FAILURE, tag=tag, request_id=request_id)
            except Exception as db_error:
                logger.exception(f"Failed to update job status to FAILURE for job '{job_name}': {db_error}")

    async def build_and_push_images(self, path: Path) -> List[str]:
        job_names = []
        request_id = str(uuid4())
        logger.info(f"Generated request_id: {request_id} for build_and_push_images")

        with open(path, "r") as file:
            pipeline = parse_image_build_pipeline(file.read())
            for item in pipeline.images:
                if item.request is None:
                    raise ValueError("Kaniko builds require a project download request")
                job_name = await self.build_and_push_single_image(
                    request=item.request,
                    image_version=item.image_version(),
                    dockerfile_content=item.dockerfile_content,
                    labels=item.labels,
                    base=None,
                    runtime_class_name=item.runtime_class_name,
                    pod_resources=item.resources,
                    request_id=request_id,
                )
                job_names.append(job_name)

        return job_names
