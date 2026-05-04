from asyncio import create_task, sleep, timeout
from os import environ as env
from pathlib import Path
from typing import Optional
from uuid import uuid4

from idegym.api.image_build import ImageBuildSpec
from idegym.api.status import Status
from idegym.backend.utils.kubernetes_client import build_and_push_image_with_kaniko, get_job_status
from idegym.image.builder import Image
from idegym.orchestrator.database.database import get_db_session, save_job_status, update_job_status
from idegym.utils import __version__
from idegym.utils.logging import get_logger
from idegym.utils.path import get_base_filename

logger = get_logger(__name__)

__DOCKER_REPOSITORY__ = env.get("DOCKER_REGISTRY", "ghcr.io/jetbrains-research/idegym")


class IdeGYMKanikoDockerAPI:
    def __init__(
        self,
        namespace: str = "idegym",
        job_timeout: float = 2400,
        insecure_registry: bool = False,
        node_pool_taint_key: Optional[str] = None,
        node_pool_preference_weight: int = 100,
    ):
        self._namespace = namespace
        self._job_timeout = job_timeout
        self._insecure_registry = insecure_registry
        self._node_pool_taint_key = node_pool_taint_key
        self._node_pool_preference_weight = node_pool_preference_weight

    async def build_and_push_single_image(
        self,
        spec: ImageBuildSpec,
        request_id: Optional[str] = None,
    ) -> str:
        registry = __DOCKER_REPOSITORY__
        if spec.name:
            image_name = spec.name
        elif spec.request is not None:
            image_name = get_base_filename(spec.request.descriptor.name)
        else:
            image_name = f"image-{spec.image_version()[:8]}"

        tag = f"{registry}/{image_name}:{spec.image_version()}"
        idegym_version = env.get("IDEGYM_VERSION") or __version__

        logger.info(f"Building image: {tag}")
        if spec.request is not None:
            logger.info(f"Download request: {spec.request.descriptor.url}, {spec.request.descriptor.name}")

        resources = (
            spec.resources.model_dump(
                by_alias=True,
                exclude_none=True,
            )
            if spec.resources
            else None
        )

        job_name = await build_and_push_image_with_kaniko(
            request=spec.request,
            tag=tag,
            service_version=idegym_version,
            dockerfile_content=spec.dockerfile_content,
            labels=spec.labels,
            namespace=self._namespace,
            ttl_seconds_after_finished=300,
            runtime_class_name=spec.runtime_class_name,
            resources=resources,
            insecure_registry=self._insecure_registry,
            node_pool_taint_key=self._node_pool_taint_key,
            node_pool_preference_weight=self._node_pool_preference_weight,
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
        except Exception:
            logger.exception(f"Error monitoring job '{job_name}'. Request ID: {request_id}")
            try:
                async with get_db_session() as db:
                    await update_job_status(db, job_name, status=Status.FAILURE, tag=tag, request_id=request_id)
            except Exception as db_error:
                logger.exception(f"Failed to update job status to FAILURE for job '{job_name}': {db_error}")

    async def build_and_push_images(self, path: Path) -> list[str]:
        job_names = []
        request_id = str(uuid4())
        logger.info(f"Generated request_id: {request_id} for build_and_push_images")

        images = Image.load_all(path.read_text())
        logger.info(f"Parsed {len(images)} image definition(s) from YAML")

        for image in images:
            spec = image.to_spec()
            job_name = await self.build_and_push_single_image(spec, request_id=request_id)
            job_names.append(job_name)

        return job_names
