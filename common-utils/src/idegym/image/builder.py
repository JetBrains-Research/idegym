from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from idegym.api.docker import BaseImage
from idegym.api.image_build import ImageBuildPipeline, ImageBuildSpec

from .plugin import BuildContext, Plugin


def _run_block(commands: Tuple[str, ...]) -> str:
    filtered = [command.strip() for command in commands if command.strip()]
    if not filtered:
        return ""
    body = " && \\\n    ".join(filtered)
    return f"RUN set -eux; \\\n    {body}"


@dataclass(frozen=True, slots=True)
class Image:
    base: str
    name: Optional[str] = None
    _plugins: Tuple[Plugin, ...] = ()
    _commands: Tuple[str, ...] = ()
    _platforms: Tuple[str, ...] = ()
    _runtime_class_name: str = "gvisor"
    _resources: Optional[Dict[str, Any]] = None

    @classmethod
    def from_base(cls, base: str | BaseImage, *, name: Optional[str] = None) -> "Image":
        image = base.value if isinstance(base, BaseImage) else base
        return cls(base=image, name=name)

    def named(self, name: str) -> "Image":
        return replace(self, name=name)

    def with_plugin(self, plugin: Plugin) -> "Image":
        if not isinstance(plugin, Plugin):
            raise TypeError("Plugin must implement apply(ctx) and render(ctx)")
        return replace(self, _plugins=(*self._plugins, plugin))

    def run_commands(self, *commands: str) -> "Image":
        if not commands:
            return self
        return replace(self, _commands=(*self._commands, *commands))

    def with_platforms(self, *platforms: str) -> "Image":
        return replace(self, _platforms=platforms)

    def with_runtime(
        self,
        *,
        runtime_class_name: Optional[str] = None,
        resources: Optional[Dict[str, Any]] = None,
    ) -> "Image":
        return replace(
            self,
            _runtime_class_name=runtime_class_name or self._runtime_class_name,
            _resources=resources if resources is not None else self._resources,
        )

    def to_spec(self) -> ImageBuildSpec:
        ctx = BuildContext(base=self.base)
        fragments: list[str] = []
        for plugin in self._plugins:
            ctx = plugin.apply(ctx)
            fragment = plugin.render(ctx).strip()
            if fragment:
                fragments.append(fragment)

        dockerfile_content = self._render_dockerfile(ctx, tuple(fragments))
        return ImageBuildSpec(
            name=self.name,
            request=ctx.request,
            dockerfile_content=dockerfile_content,
            labels=dict(ctx.labels),
            context_path=ctx.context_path,
            platforms=list(self._platforms),
            runtime_class_name=self._runtime_class_name,
            resources=self._resources,
        )

    def _render_dockerfile(
        self,
        ctx: BuildContext,
        fragments: Tuple[str, ...],
    ) -> str:
        lines = [f"FROM {self.base}", "", 'SHELL ["/bin/bash", "-c"]', "", "USER root"]

        if ctx.request is not None:
            lines.extend(
                [
                    "",
                    "ARG IDEGYM_PROJECT_ARCHIVE_URL",
                    "ARG IDEGYM_PROJECT_ARCHIVE_PATH",
                    "ARG IDEGYM_AUTH_TOKEN",
                    "ARG IDEGYM_AUTH_TYPE",
                    "",
                    'ENV IDEGYM_PROJECT_ARCHIVE_URL="$IDEGYM_PROJECT_ARCHIVE_URL"',
                    'ENV IDEGYM_PROJECT_ARCHIVE_PATH="$IDEGYM_PROJECT_ARCHIVE_PATH"',
                ]
            )

        lines.extend(["", f'ENV IDEGYM_PROJECT_ROOT="{ctx.project_root}"'])

        for fragment in fragments:
            lines.extend(["", fragment])

        lines.extend(["", f"USER {ctx.current_user}"])

        commands_block = _run_block(self._commands)
        if commands_block:
            lines.extend(["", commands_block])

        return "\n".join(lines).strip() + "\n"

    def to_pipeline(self) -> ImageBuildPipeline:
        return ImageBuildPipeline(images=[self.to_spec()])

    def to_yaml(self) -> str:
        return self.to_pipeline().to_yaml()

    def write_yaml(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(self.to_yaml())
        return target

    def compile(self) -> ImageBuildSpec:
        return self.to_spec()
