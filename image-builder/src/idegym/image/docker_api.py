from collections.abc import Iterable
from pathlib import Path
from typing import Optional

import yaml
from idegym.api.docker import BaseImage
from idegym.api.download import Authorization, DownloadRequest
from idegym.api.git import GitRepositoryResource, GitRepositorySnapshot
from idegym.image.docker_service import DockerService
from idegym.utils.hashing import md5
from python_on_whales import Image as DockerImage


class IdeGYMDockerAPI:
    def __init__(self, registry: Optional[str] = None):
        self._docker_service = DockerService(registry=registry) if registry else DockerService()
        self._docker_service.login()

    def build(
        self,
        project: GitRepositoryResource | GitRepositorySnapshot,
        base: BaseImage = BaseImage.DEFAULT,
        auth: Optional[Authorization] = None,
        commands: None | str | Iterable[str] = None,
        platforms: Optional[list[str]] = None,
    ) -> DockerImage:
        request = DownloadRequest(
            descriptor=project.descriptor(),
            auth=auth if auth is not None else Authorization(),
        )
        labels = self._docker_service.labels(project)
        commands_str = "\n".join(commands) if commands and not isinstance(commands, str) else (commands or "")
        image_version = md5(self._docker_service.hash(project), base.value, commands_str)
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
    ) -> list[DockerImage]:
        images = []
        with open(path, "r") as file:
            pipeline: dict[str, list[dict]] = yaml.safe_load(file)
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
