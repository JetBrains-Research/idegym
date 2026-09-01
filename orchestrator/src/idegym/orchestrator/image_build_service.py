from asyncio import create_task, sleep, timeout
from os import environ as env
from pathlib import Path
from typing import Optional
from uuid import uuid4

from idegym.api.image_build import ImageBuildSpec, check_registry_allowed
from idegym.api.status import Status
from idegym.backend.utils.image_builder import BuildHandle, ImageBuilder
from idegym.image.builder import Image
from idegym.orchestrator.database.database import get_db_session, save_job_status, update_job_status
from idegym.utils import __version__
from idegym.utils.logging import get_logger
from idegym.utils.path import get_base_filename

logger = get_logger(__name__)

__DOCKER_REPOSITORY__ = env.get("DOCKER_REGISTRY", "ghcr.io/jetbrains-research/idegym")


class ImageBuildService:
    """Builder-agnostic orchestration of image builds.

    Owns the parts shared across backends — tag/version construction, persisting build
    status to the DB, and the polling loop — and delegates the actual build to an injected
    `ImageBuilder`. The backend-specific `BuildHandle` is kept in memory for
    the monitoring task; only ``handle.name`` is persisted (as ``JobStatusRecord.job_name``)
    and returned to clients.
    """

    def __init__(
        self,
        builder: ImageBuilder,
        namespace: str = "idegym",
        job_timeout: Optional[float] = None,
        allowed_registry_prefixes: Optional[list[str]] = None,
    ):
        self._builder = builder
        self._namespace = namespace
        # Default the monitor timeout to whatever the backend advertises so a backend with a
        # configurable build timeout (e.g. Cloud Build) is not cut off prematurely.
        self._job_timeout = job_timeout if job_timeout is not None else builder.monitor_timeout()
        self._allowed_registry_prefixes = list(allowed_registry_prefixes or [])

    def resolve_tag(self, spec: ImageBuildSpec) -> str:
        """Return the destination tag for ``spec``, honouring a caller-chosen one.

        With neither ``tag`` nor ``registry`` set this is exactly the historical behaviour: the
        deployment's registry, a name derived from the spec, and the content hash as the version.

        A caller-supplied destination is checked against the configured allowlist, because otherwise
        it would mean pushing anywhere the builder's service account can write. A consumer that keeps
        its own content-addressed tags in its own registry needs this to make image preparation
        idempotent and resumable — without it, its "already pushed?" check has nothing to look up.
        """
        if spec.name:
            image_name = spec.name
        elif spec.request is not None:
            image_name = get_base_filename(spec.request.descriptor.name)
        else:
            image_name = f"image-{spec.image_version()[:8]}"

        if spec.tag is not None:
            check_registry_allowed(spec.tag, self._allowed_registry_prefixes)
            return spec.tag

        registry = spec.registry or __DOCKER_REPOSITORY__
        tag = f"{registry}/{image_name}:{spec.version or spec.image_version()}"
        if spec.registry is not None:
            check_registry_allowed(tag, self._allowed_registry_prefixes)
        return tag

    async def build_and_push_single_image(
        self,
        spec: ImageBuildSpec,
        request_id: Optional[str] = None,
    ) -> str:
        tag = self.resolve_tag(spec)
        idegym_version = env.get("IDEGYM_VERSION") or __version__

        logger.info(f"Building image: {tag}")
        if spec.request is not None:
            logger.info(f"Download request: {spec.request.descriptor.url}, {spec.request.descriptor.name}")

        handle = await self._builder.submit_build(
            tag,
            spec,
            namespace=self._namespace,
            service_version=idegym_version,
        )

        create_task(self.monitor_image_building_job(handle, tag, request_id, warnings=spec.warnings))

        return handle.name

    async def monitor_image_building_job(
        self,
        handle: BuildHandle,
        tag: str,
        request_id: Optional[str] = None,
        warnings: Optional[list[str]] = None,
    ) -> None:
        job_name = handle.name
        # Caveats about this build are recorded on the job so they survive it: a warning logged at
        # submit time is long gone by the time anyone asks about the image. Two sources — the spec,
        # for what compiling the definition found, and the handle, for what the backend had to do.
        details = "\n".join([*(warnings or []), *handle.warnings]) or None
        # Prefer the deadline the backend actually granted this build; the service-wide default only
        # describes the deployment, so a build given a longer per-request timeout would otherwise be
        # recorded as failed while still running.
        job_timeout = handle.monitor_timeout if handle.monitor_timeout is not None else self._job_timeout
        try:
            async with get_db_session() as db:
                await save_job_status(
                    db,
                    job_name,
                    status=Status.IN_PROGRESS,
                    tag=tag,
                    details=details,
                    request_id=request_id,
                )

            try:
                async with timeout(job_timeout):
                    status = await self._builder.get_status(handle)
                    while status == Status.IN_PROGRESS:
                        await sleep(2)
                        status = await self._builder.get_status(handle)

                    async with get_db_session() as db:
                        await update_job_status(db, job_name, status=status, tag=tag, request_id=request_id)

                    if status == Status.SUCCESS:
                        logger.info(f"Job '{job_name}' finished successfully. Request ID: {request_id}")
                    else:
                        logger.error(
                            f"Job '{job_name}' was terminated with status '{status}'. Request ID: {request_id}"
                        )
            except TimeoutError:
                logger.error(f"Job '{job_name}' monitoring timed out after {job_timeout}s. Request ID: {request_id}")
                async with get_db_session() as db:
                    await update_job_status(db, job_name, status=Status.FAILURE, tag=tag, request_id=request_id)
        except Exception:
            logger.exception(f"Error monitoring job '{job_name}'. Request ID: {request_id}")
            try:
                async with get_db_session() as db:
                    await update_job_status(db, job_name, status=Status.FAILURE, tag=tag, request_id=request_id)
            except Exception:
                logger.exception(f"Failed to update job status to FAILURE for job '{job_name}'")

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
