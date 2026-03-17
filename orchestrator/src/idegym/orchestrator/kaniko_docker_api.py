from asyncio import create_task, sleep, timeout
from hashlib import md5
from os import environ as env
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union
from uuid import uuid4

from idegym.api.docker import BaseImage
from idegym.api.download import Authorization, DownloadRequest
from idegym.api.git import GitRepositoryResource, GitRepositorySnapshot
from idegym.api.status import Status
from idegym.backend.utils.kubernetes_client import build_and_push_image_with_kaniko, clean_up_after_job, get_job_status
from idegym.orchestrator.database.database import get_db_session, save_job_status, update_job_status
from idegym.utils import __version__
from idegym.utils.dict import walk
from idegym.utils.dockerfile import render_dockerfile
from idegym.utils.logging import get_logger
from idegym.utils.path import get_base_filename
from yaml import safe_load as parse

logger = get_logger(__name__)

__DOCKER_REPOSITORY__ = "ghcr.io/jetbrains-research/idegym"


def isiterable(value: Any) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, str)


class IdeGYMKanikoDockerAPI:
    def __init__(
        self,
        namespace: str = "idegym",
        job_timeout: float = 2400,
    ):
        self._namespace = namespace
        self._job_timeout = job_timeout

    @staticmethod
    def hash(project: GitRepositorySnapshot | GitRepositoryResource) -> str:
        digest = md5()
        identifiers = [str(value) for value in walk(project.model_dump()) if value is not None]
        for identifier in identifiers:
            digest.update(identifier.encode())
        return digest.hexdigest()

    @staticmethod
    def labels(value: GitRepositoryResource | GitRepositorySnapshot) -> Dict[str, str]:
        if isinstance(value, GitRepositoryResource):
            labels = IdeGYMKanikoDockerAPI.labels(value.snapshot)
            return {**labels, "idegym.repository.resource": value.path}
        elif isinstance(value, GitRepositorySnapshot):
            labels = {"idegym.repository.url": value.repository.url}
            return {**labels, "idegym.repository.revision": value.reference}
        else:
            raise ValueError("Unsupported type!")

    async def build_and_push_single_image(
        self,
        project: GitRepositoryResource | GitRepositorySnapshot,
        auth: Optional[Authorization] = None,
        base: BaseImage = BaseImage.DEFAULT,
        commands: Union[None, str, Iterable[str]] = None,
        runtime_class_name: str = "gvisor",
        pod_resources: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> str:
        request = DownloadRequest(
            descriptor=project.descriptor(),
            auth=auth if auth is not None else Authorization(),
        )
        # Coerce commands into `str`
        commands: Union[str, Iterable[str]] = [] if commands is None else commands
        commands: str = "\n".join(commands) if isiterable(commands) else commands
        labels = self.labels(project)
        image_version = self.hash(project)

        dockerfile_content = render_dockerfile(commands=commands)

        logger.info(f"Download request: {request.descriptor.url}, {request.descriptor.name}")

        registry = __DOCKER_REPOSITORY__
        image_name = get_base_filename(request.descriptor.name)
        tag = f"{registry}/{image_name}:{image_version}"
        idegym_version = env.get("IDEGYM_VERSION") or __version__

        job_name = await build_and_push_image_with_kaniko(
            request=request,
            tag=tag,
            base=base.value,
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
        async with get_db_session() as db:
            await save_job_status(db, job_name, status=Status.IN_PROGRESS, tag=tag, request_id=request_id)

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
                logger.error(f"Job '{job_name}' was terminated with status '{status}'. Request ID: {request_id}")

            await clean_up_after_job(job_name, self._namespace)

    async def build_and_push_images(self, path: Path) -> List[str]:
        job_names = []
        request_id = str(uuid4())
        logger.info(f"Generated request_id: {request_id} for build_and_push_images")

        with open(path, "r") as file:
            pipeline: Dict[str, List[Dict[str, ...]]] = parse(file)
            items = pipeline.get("images", [])
            for item in items:
                auth = item.pop("auth", {})
                if type(auth) is not dict:
                    raise TypeError(f"Unable to parse 'auth' field. Expected a dictionary, got: {auth}")
                base = item.pop("base", "default").upper().replace("-", "_")
                if type(base) is not str:
                    raise TypeError(f"Unable to parse 'base' field. Expected a string, got: {base}")
                commands = item.pop("commands", "")
                if type(commands) is not str:
                    raise TypeError(f"Unable to parse 'commands' field. Expected a string, got: {commands}")
                runtime_class_name = item.pop("runtime_class_name", "gvisor")
                if type(runtime_class_name) is not str:
                    raise TypeError(
                        f"Unable to parse 'runtime_class_name' field. Expected a string, got: {runtime_class_name}"
                    )
                pod_resources = item.pop("resources", None)
                if pod_resources is not None and type(pod_resources) is not dict:
                    raise TypeError(f"Unable to parse 'resources' field. Expected a dictionary, got: {pod_resources}")
                project = item.pop("project", {})
                if type(project) is not dict:
                    raise TypeError(f"Unable to parse 'project' field. Expected a dictionary, got: {project}")
                model = GitRepositoryResource if "path" in project else GitRepositorySnapshot
                job_name = await self.build_and_push_single_image(
                    project=model(**project),
                    auth=Authorization(**auth),
                    base=BaseImage[base],
                    commands=commands,
                    runtime_class_name=runtime_class_name,
                    pod_resources=pod_resources,
                    request_id=request_id,
                )
                job_names.append(job_name)

        return job_names
