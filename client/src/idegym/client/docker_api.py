from pathlib import Path
from typing import Dict, Iterable, List, Optional, Union

import yaml
from idegym.api.docker import BaseImage
from idegym.api.download import Authorization, DownloadRequest
from idegym.api.git import GitRepositoryResource, GitRepositorySnapshot
from idegym.client.docker_service import DockerService
from python_on_whales import Image as DockerImage


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
    ) -> DockerImage:
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

    def build_image(
        self,
        image,
        push: bool = False,
    ) -> DockerImage:
        built_image = self._docker_service.build_image(image)
        if push:
            self.push(built_image)
        return built_image

    def push(self, *image: DockerImage) -> None:
        images = tuple(image)
        self._docker_service.push(images)

    def build_and_push_from_yaml(
        self,
        path: Path,
        multiplatform: bool = True,
        push: bool = False,
    ) -> List[DockerImage]:
        images = []
        with open(path, "r") as file:
            pipeline: Dict[str, List[Dict[str, ...]]] = yaml.safe_load(file)
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
                platforms = item.pop("platforms", ["linux/amd64", "linux/arm64"] if multiplatform else [])
                if type(platforms) is not list:
                    raise TypeError(f"Unable to parse 'platforms' field. Expected a list, got: {platforms}")
                project = item.pop("project", {})
                if type(project) is not dict:
                    raise TypeError(f"Unable to parse 'project' field. Expected a dictionary, got: {project}")
                model = GitRepositoryResource if "path" in project else GitRepositorySnapshot
                image = self.build(
                    project=model(**project),
                    base=BaseImage[base],
                    auth=Authorization(**auth),
                    commands=commands,
                    platforms=platforms,
                )
                images.append(image)
        if push:
            self.push(*images)
        return images
