from pathlib import Path
from typing import Any, Optional, Self

from idegym.api.docker import BaseImage
from idegym.api.image_build import ImageBuildSpec
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.image.plugin import BuildContext, PluginBase
from idegym.image.serialization import deserialize_plugin, dump_images, load_images, serialize_plugin
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def _run_block(commands: tuple[str, ...]) -> str:
    filtered = [command.strip() for command in commands if command.strip()]
    if not filtered:
        return ""
    body = " && \\\n    ".join(filtered)
    return f"RUN set -eux; \\\n    {body}"


class Image(BaseModel):
    base: str = Field(min_length=1)
    name: Optional[str] = Field(default=None)
    plugins: tuple[PluginBase, ...] = Field(default_factory=tuple)
    commands: tuple[str, ...] = Field(default_factory=tuple)
    platforms: tuple[str, ...] = Field(default_factory=tuple)
    runtime_class_name: str = Field(default="gvisor", min_length=1)
    resources: Optional[dict[str, Any]] = Field(default=None)

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    @field_validator("plugins", mode="before")
    @classmethod
    def parse_plugins(cls, value: Any) -> tuple[PluginBase, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"Image 'plugins' must be a list or tuple, got {type(value).__name__}")

        plugins: list[PluginBase] = []
        for item in value:
            if isinstance(item, dict):
                plugins.append(deserialize_plugin(item))
                continue
            if not isinstance(item, PluginBase):
                raise TypeError("Plugin must inherit from PluginBase")
            plugins.append(item)
        return tuple(plugins)

    @field_serializer("plugins")
    def dump_plugins(self, plugins: tuple[PluginBase, ...]) -> list[dict[str, Any]]:
        return [serialize_plugin(plugin) for plugin in plugins]

    @classmethod
    def from_base(cls, base: str | BaseImage, *, name: Optional[str] = None) -> Self:
        image = base.value if isinstance(base, BaseImage) else base
        return cls(base=image, name=name)

    def named(self, name: str) -> Self:
        return self.model_copy(update={"name": name})

    def with_plugin(self, plugin: PluginBase) -> Self:
        if not isinstance(plugin, PluginBase):
            raise TypeError("Plugin must inherit from PluginBase")
        return self.model_copy(update={"plugins": (*self.plugins, plugin)})

    def run_commands(self, *commands: str) -> Self:
        if not commands:
            return self
        return self.model_copy(update={"commands": (*self.commands, *commands)})

    def with_platforms(self, *platforms: str) -> Self:
        return self.model_copy(update={"platforms": tuple(platforms)})

    def with_runtime(
        self,
        *,
        runtime_class_name: Optional[str] = None,
        resources: Optional[dict[str, Any]] = None,
    ) -> Self:
        return self.model_copy(
            update={
                "runtime_class_name": runtime_class_name or self.runtime_class_name,
                "resources": resources if resources is not None else self.resources,
            }
        )

    def to_spec(self) -> ImageBuildSpec:
        ctx = BuildContext(base=self.base)
        fragments: list[str] = []
        for plugin in self.plugins:
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
            platforms=list(self.platforms),
            runtime_class_name=self.runtime_class_name,
            resources=self.resources,
        )

    def _render_dockerfile(
        self,
        ctx: BuildContext,
        fragments: tuple[str, ...],
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

        commands_block = _run_block(self.commands)
        if commands_block:
            lines.extend(["", commands_block])

        return "\n".join(lines).strip() + "\n"

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, definition: dict[str, Any]) -> Self:
        return cls.model_validate(definition)

    @classmethod
    def from_yaml(cls, value: str | bytes | dict[str, Any]) -> Self:
        images = load_images(value, cls)
        if len(images) != 1:
            raise ValueError(f"Expected exactly one image definition, got {len(images)}")
        return images[0]

    @classmethod
    def load_all(cls, value: str | bytes | dict[str, Any]) -> tuple[Self, ...]:
        return load_images(value, cls)

    def to_yaml(self) -> str:
        return dump_images((self,))

    def write_yaml(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(self.to_yaml())
        return target

    def build(self, registry: Optional[str] = None) -> Any:
        return IdeGYMDockerAPI(registry=registry).build_image(self)

    def compile(self) -> ImageBuildSpec:
        return self.to_spec()
