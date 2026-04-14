import re
from collections.abc import Iterable
from os import environ as env
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Final, Optional
from uuid import uuid4

from idegym.api.docker import BaseImage, ContainerConfig
from idegym.api.download import DownloadRequest
from idegym.api.git import GitRepository, GitRepositoryResource, GitRepositorySnapshot
from idegym.image.dockerfile import render_dockerfile
from idegym.utils import __version__ as library_version
from idegym.utils.dict import walk
from idegym.utils.hashing import md5
from idegym.utils.logging import get_logger
from idegym.utils.path import get_base_filename
from python_on_whales import Container, DockerClient
from python_on_whales import Image as DockerImage

_CONTAINER_PORT = "8000/tcp"
_CONTAINER_VOLUME_PATH = "/docker-entrypoint.d"

Port = int | list[int] | tuple[str, int] | None
logger = get_logger(__name__)


def isiterable(value: Any) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, str)


class DockerService:
    CLIENT: Final[DockerClient] = DockerClient()
    REGISTRY: Final[str] = "ghcr.io/jetbrains-research/idegym"
    PATTERN: Final[re.Pattern] = re.compile("(?:\x1b[@-_]|[\x80-\x9f])[0-?]*[ -/]*[@-~]")

    def __init__(self, client: DockerClient = CLIENT, registry: str = REGISTRY):
        self._client: DockerClient = client
        self._registry: str = registry

    def login(self):
        username = env.get("IDEGYM_DOCKER_USERNAME")
        password = env.get("IDEGYM_DOCKER_PASSWORD")
        if username is None or password is None:
            logger.debug("Skipping login to Docker registry. No credentials provided.")
            return

        server, _ = self._registry.split("/", 1)
        logger.debug("Logging into Docker registry.", server=server)
        self._client.login(
            server=server,
            username=username,
            password=password,
        )

    @staticmethod
    def hash(project: GitRepositorySnapshot | GitRepositoryResource) -> str:
        identifiers = [str(value) for value in walk(project.model_dump()) if value is not None]
        return md5(*identifiers)

    @staticmethod
    def labels(value: GitRepository | GitRepositorySnapshot | GitRepositoryResource) -> dict[str, str]:
        match value:
            case repository if isinstance(value, GitRepository):
                return {"idegym.repository.url": repository.url}
            case snapshot if isinstance(value, GitRepositorySnapshot):
                labels = DockerService.labels(snapshot.repository)
                return {**labels, "idegym.repository.revision": snapshot.reference}
            case resource if isinstance(value, GitRepositoryResource):
                labels = DockerService.labels(resource.snapshot)
                return {**labels, "idegym.repository.resource": resource.path}
            case _:
                raise ValueError(f"Unsupported type: {type(value).__name__}")

    def build_image(
        self,
        image,
    ) -> DockerImage:
        compiled = image.to_spec()
        return self.build(
            request=compiled.request,
            image_version=compiled.image_version(),
            image_base=None,
            labels=compiled.labels,
            image_name=compiled.name,
            context_path=compiled.context_path,
            platforms=compiled.platforms,
            dockerfile_content=compiled.dockerfile_content,
        )

    def build(
        self,
        request: Optional[DownloadRequest],
        image_version: str,
        image_base: Optional[str] = BaseImage.DEFAULT.value,
        service_version: str = library_version,
        commands: None | str | Iterable[str] = None,
        labels: Optional[dict[str, str]] = None,
        registry: Optional[str] = None,
        image_name: Optional[str] = None,
        context_path: str = ".",
        platforms: Optional[list[str]] = None,
        dockerfile_content: Optional[str] = None,
    ) -> DockerImage:
        commands = [] if commands is None else commands
        commands = "\n".join(commands) if isiterable(commands) else commands
        platforms = None if not platforms else platforms
        labels = {} if labels is None else labels
        rendered = dockerfile_content if dockerfile_content else render_dockerfile(commands=commands)
        temporary_dir = context_path if context_path != "." else None
        with NamedTemporaryFile(mode="w", prefix="Dockerfile.", dir=temporary_dir, delete=True) as dockerfile:
            dockerfile.write(rendered)
            dockerfile.flush()

            resolved_image_name = image_name or (get_base_filename(request.descriptor.name) if request else None)
            if resolved_image_name is None:
                raise ValueError("Image name is required when build request is not provided")

            tag = f"{self._registry}/{resolved_image_name}:{image_version}"
            build_args = {
                "IDEGYM_REGISTRY": registry,
                "IDEGYM_VERSION": service_version,
            }
            if request is not None:
                build_args.update(
                    {
                        "IDEGYM_PROJECT_ARCHIVE_URL": request.descriptor.url,
                        "IDEGYM_PROJECT_ARCHIVE_PATH": request.descriptor.name,
                        "IDEGYM_AUTH_TYPE": request.auth.type,
                        "IDEGYM_AUTH_TOKEN": request.auth.token,
                    }
                )
            if image_base is not None:
                build_args["IDEGYM_BASE"] = image_base

            build_args = {k: v for k, v in build_args.items() if v is not None}
            logs: Iterable[str] = self._client.build(
                context_path=context_path,
                file=dockerfile.name,
                tags=[tag],
                build_args=build_args,
                labels=labels,
                platforms=platforms,
                progress="plain",
                stream_logs=True,
                load=True,
            )

            logger.debug("Building image", tag=tag)
            for line in logs:
                clean = self.PATTERN.sub(
                    string=line,
                    repl="",
                )
                if message := clean.strip():
                    logger.debug(message)
            image = self._client.image.inspect(tag)
            logger.info("Built image", id=image.id[:20], tag=tag)

            return image

    def push(self, images: Iterable[DockerImage]):
        tags = [tag for image in images for tag in image.repo_tags]
        logger.info(f"Pushing image tags: {tags}")
        if generator := self._client.image.push(tags, stream_logs=True):
            for image, line in generator:
                message = line.decode(errors="replace").strip()
                logger.debug(message, image=image)
        logger.info(f"Pushed image tags: {tags}")

    def run(
        self,
        image: DockerImage,
        port: Optional[Port] = None,
        scripts: Optional[list[Path]] = None,
        config: Optional[ContainerConfig] = None,
    ) -> Container:
        scripts = [] if scripts is None else scripts
        configs = {} if config is None else config.model_dump()
        volumes = [(path, f"{_CONTAINER_VOLUME_PATH}/{path.name}", "ro") for path in scripts]

        ports = [(port, _CONTAINER_PORT)] if port else [(_CONTAINER_PORT,)]

        return self._client.run(
            image=image,
            name=f"idegym-{uuid4()}",
            publish=ports,
            volumes=volumes,
            detach=True,
            **configs,
        )
