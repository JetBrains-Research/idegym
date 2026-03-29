from pathlib import Path
from typing import Iterable, List, Optional, Union

from idegym.api.docker import BaseImage
from idegym.api.download import Authorization, DownloadRequest
from idegym.api.git import GitRepositoryResource, GitRepositorySnapshot
from idegym.api.image_build import parse_image_build_pipeline
from idegym.client.docker_service import DockerService
from python_on_whales import Image


class IdeGYMDockerAPI:
    """
    API for Docker operations including building and pushing images.
    """

    def __init__(self, registry: Optional[str] = None):
        self._docker_service = DockerService(registry=registry) if registry else DockerService()
        self._docker_service.login()

    def build(
        self,
        project: GitRepositoryResource | GitRepositorySnapshot,
        base: BaseImage = BaseImage.DEFAULT,
        auth: Optional[Authorization] = None,
        commands: Union[None, str, Iterable[str]] = None,
        platforms: Optional[List[str]] = None,
    ) -> Image:
        request = DownloadRequest(
            descriptor=project.descriptor(),
            auth=auth if auth is not None else Authorization(),
        )
        labels = self._docker_service.labels(project)
        image_version = self._docker_service.hash(project)
        return self._docker_service.build(
            request=request,
            image_version=image_version,
            image_base=base.value,
            commands=commands,
            labels=labels,
            platforms=platforms,
        )

    def push(self, *image: Image) -> None:
        images = tuple(image)
        self._docker_service.push(images)

    def build_and_push_from_yaml(
        self,
        path: Path,
        multiplatform: bool = True,
        push: bool = False,
    ) -> List[Image]:
        images = []
        with open(path, "r") as file:
            pipeline = parse_image_build_pipeline(file.read())
            for item in pipeline.images:
                platforms = item.platforms or (["linux/amd64", "linux/arm64"] if multiplatform else [])
                image = self._docker_service.build(
                    request=item.request,
                    image_version=item.image_version(),
                    image_base=None,
                    labels=item.labels,
                    image_name=item.name,
                    context_path=item.context_path,
                    platforms=platforms,
                    dockerfile_content=item.dockerfile_content,
                )
                images.append(image)
        if push:
            self.push(*images)
        return images
