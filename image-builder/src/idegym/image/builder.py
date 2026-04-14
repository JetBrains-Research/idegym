from pathlib import Path
from typing import Any, Optional, Self

import idegym.image.plugins  # noqa: F401 — ensures built-in plugins are registered before deserialization
from idegym.api.docker import BaseImage
from idegym.api.image_build import ImageBuildSpec
from idegym.api.type import OCIImageName
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.image.plugin import BuildContext, PluginBase
from idegym.image.serialization import deserialize_plugin, dump_images, load_images, serialize_plugin
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_serializer, field_validator

# TypeAdapter reuses the OCIImageName constraints without duplicating the regex.
# Needed because model_copy() bypasses Pydantic field validation.
_OCI_NAME_VALIDATOR = TypeAdapter(OCIImageName)


def _run_block(commands: tuple[str, ...]) -> str:
    filtered = [command.strip() for command in commands if command.strip()]
    if not filtered:
        return ""
    body = " && \\\n    ".join(filtered)
    return f"RUN set -eux; \\\n    {body}"


class Image(BaseModel):
    """Fluent, immutable builder for container images.

    Construct an ``Image`` with a base image reference, chain builder methods to attach plugins
    and commands, then call ``to_spec()`` to compile a ``ImageBuildSpec`` that can be passed to
    a build backend (Kaniko, Docker, etc.).

    Images can be serialized to/from YAML (``to_yaml`` / ``from_yaml`` / ``load_all``) and to/from
    plain dicts (``to_dict`` / ``from_dict``).

    Example::

        image = (
            Image.from_base("debian:bookworm-slim", name="my-image")
            .with_plugin(BaseSystem())
            .with_plugin(User(username="dev"))
            .with_plugin(Project.from_git(url="https://github.com/org/repo.git", ref="main"))
            .run_commands("cd ~/work && pip install -e .")
        )
        spec = image.to_spec()
    """

    base: str = Field(min_length=1)
    name: Optional[OCIImageName] = Field(default=None)
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
        """Create an image from a base image reference or ``BaseImage`` enum value."""
        image = base.value if isinstance(base, BaseImage) else base
        return cls(base=image, name=name)

    def named(self, name: str) -> Self:
        """Return a copy with the image name set, validating OCI naming rules."""
        _OCI_NAME_VALIDATOR.validate_python(name)
        return self.model_copy(update={"name": name})

    def with_plugin(self, plugin: PluginBase) -> Self:
        """Return a copy with ``plugin`` appended to the plugin list."""
        if not isinstance(plugin, PluginBase):
            raise TypeError("Plugin must inherit from PluginBase")
        return self.model_copy(update={"plugins": (*self.plugins, plugin)})

    def run_commands(self, *commands: str) -> Self:
        """Return a copy with additional shell commands appended.

        Commands are emitted as a single ``RUN set -eux`` block at the end of the Dockerfile,
        after all plugin fragments. Each command is a bare shell statement — do not include a
        ``RUN`` prefix.
        """
        if not commands:
            return self
        return self.model_copy(update={"commands": (*self.commands, *commands)})

    def pip_install(self, *packages: str) -> Self:
        """Return a copy with a ``pip install`` command for the given packages appended."""
        if not packages:
            return self
        return self.run_commands(f"pip install {' '.join(packages)}")

    def with_platforms(self, *platforms: str) -> Self:
        """Return a copy targeting the given build platforms (e.g. ``linux/amd64``)."""
        return self.model_copy(update={"platforms": tuple(platforms)})

    def with_runtime(
        self,
        *,
        runtime_class_name: Optional[str] = None,
        resources: Optional[dict[str, Any]] = None,
    ) -> Self:
        """Return a copy with Kubernetes runtime settings overridden."""
        return self.model_copy(
            update={
                "runtime_class_name": runtime_class_name or self.runtime_class_name,
                "resources": resources if resources is not None else self.resources,
            }
        )

    def to_spec(self) -> ImageBuildSpec:
        """Compile the image definition into an ``ImageBuildSpec``.

        Runs the plugin pipeline: each plugin's ``apply()`` is called in order to accumulate the
        final ``BuildContext``, then each plugin's ``render()`` produces a Dockerfile fragment.
        The fragments and any ``run_commands`` are assembled into a complete Dockerfile.
        """
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
        """Load a single image from a YAML document or pre-parsed dict.

        The document must contain exactly one entry under the ``images`` key.
        Use ``load_all`` if the document may contain multiple images.
        """
        images = load_images(value, cls)
        if len(images) != 1:
            raise ValueError(f"Expected exactly one image definition, got {len(images)}")
        return images[0]

    @classmethod
    def load_all(cls, value: str | bytes | dict[str, Any]) -> tuple[Self, ...]:
        """Load all images from a YAML document or pre-parsed dict."""
        return load_images(value, cls)

    def to_yaml(self) -> str:
        return dump_images((self,))

    def write_yaml(self, path: str | Path) -> Path:
        """Serialize the image to YAML and write it to ``path``. Returns the resolved path."""
        target = Path(path)
        target.write_text(self.to_yaml())
        return target

    def build(self, registry: Optional[str] = None) -> Any:
        """Build the image locally using Docker. Returns a ``DockerImage``."""
        return IdeGYMDockerAPI(registry=registry).build_image(self)
