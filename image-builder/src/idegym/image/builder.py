import json
import logging as _logging
from importlib.metadata import entry_points as _entry_points
from pathlib import Path
from shlex import quote
from typing import Any, Optional, Self

_logger = _logging.getLogger(__name__)

# Load all installed image plugins (populates the @image_plugin registry).
# Failures are isolated per plugin so an optional plugin with missing deps
# does not prevent the core image builder from working.
for _ep in _entry_points(group="idegym.plugins.image"):
    try:
        _ep.load()
    except Exception:
        _logger.warning("Failed to load image plugin %r", _ep.name, exc_info=True)

from idegym.api.docker import BaseImage
from idegym.api.image_build import (
    ImageBuildSpec,
    check_build_arg_collisions,
    validate_build_arg_names,
    validate_context_uri,
    validate_image_tag,
    validate_secret_mapping,
)
from idegym.api.plugin import (
    MCP_UPSTREAMS_DIR,
    SAFE_PLUGIN_NAME_RE,
    BuildContext,
    PluginBase,
    get_plugin_type_name,
)
from idegym.api.type import OCIImageName
from idegym.image.base_dockerfile import (
    AUTH_TOKEN_ARG,
    NormalizedBase,
    local_context_sources,
    normalize_base_dockerfile,
    references_auth_token,
)
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.image.serialization import deserialize_plugin, dump_images, load_images, serialize_plugin
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_serializer, field_validator, model_validator

# TypeAdapter reuses the OCIImageName constraints without duplicating the regex.
# Needed because model_copy() bypasses Pydantic field validation.
_OCI_NAME_VALIDATOR = TypeAdapter(OCIImageName)


def _run_block(commands: tuple[str, ...]) -> str:
    filtered = [command.strip() for command in commands if command.strip()]
    if not filtered:
        return ""
    body = " && \\\n    ".join(filtered)
    return f"RUN set -eux; \\\n    {body}"


def _mcp_upstream_fragment(plugin: PluginBase, ctx: BuildContext) -> str:
    """Return a Dockerfile fragment that writes the MCP upstream config file, or empty string.

    The config file lives under ``/etc/idegym/mcp-upstreams.d/`` which is root-owned.
    If the current build user is not root, the fragment wraps the ``RUN`` with
    ``USER root`` / ``USER <current_user>`` so the write always succeeds regardless of
    where in the pipeline the plugin sits.
    """
    mcp_url = plugin.get_mcp_upstream(ctx)
    if mcp_url is None:
        return ""
    try:
        plugin_name = get_plugin_type_name(plugin)
    except KeyError:
        plugin_name = type(plugin).__name__.lower()
    if not SAFE_PLUGIN_NAME_RE.match(plugin_name):
        raise ValueError(
            f"Plugin name {plugin_name!r} is not a safe filename component. "
            "Must match ^[a-z][a-z0-9-]{0,62}$ (lowercase letters, digits, hyphens; starts with a letter)."
        )
    config = json.dumps({"url": mcp_url})
    run = _run_block(
        (
            f"mkdir -p {MCP_UPSTREAMS_DIR}",
            f"printf '%s\\n' {quote(config)} > {MCP_UPSTREAMS_DIR}/{plugin_name}.json",
        )
    )
    comment = f"# Register MCP upstream: {plugin_name}"
    if ctx.current_user == "root":
        return f"{comment}\n{run}"
    return f"{comment}\nUSER root\n{run}\nUSER {ctx.current_user}"


class Image(BaseModel):
    """Fluent, immutable builder for container images.

    Construct an ``Image`` with a base — either a registry reference (``base``) or Dockerfile text
    to compile in the same build (``base_dockerfile``) — chain builder methods to attach plugins
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

    base: Optional[str] = Field(default=None, min_length=1)
    base_dockerfile: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Dockerfile text used as the base, compiled into the same build as the plugin stages "
            "so no intermediate image is pushed. Mutually exclusive with 'base'."
        ),
    )
    base_stage: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Which stage of a multi-stage 'base_dockerfile' is the base. Defaults to the last one.",
    )
    context_uri: Optional[str] = Field(
        default=None,
        description=(
            "URI of a build context archive the caller has already staged, so the base "
            "Dockerfile's COPY/ADD sources resolve (e.g. 'gs://bucket/contexts/abc123.tar.gz')."
        ),
    )
    build_args: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Values for ARGs the base Dockerfile declares. Never credentials — a build arg's value "
            "is recorded in the image history; use 'secrets' for those."
        ),
    )
    secrets: dict[str, str] = Field(
        default_factory=dict,
        description="Maps a Dockerfile secret id to a Secret Manager resource name. Names only, never values.",
    )
    tag: Optional[str] = Field(
        default=None,
        description="Fully qualified destination tag. Overrides registry/name/version; subject to the "
        "deployment's registry allowlist.",
    )
    registry: Optional[str] = Field(default=None, description="Destination registry prefix. Excludes 'tag'.")
    version: Optional[str] = Field(
        default=None,
        description="Destination version component. Defaults to the content hash, which is what makes "
        "resubmitting an identical definition a no-op.",
    )
    timeout_seconds: Optional[int] = Field(
        default=None, ge=1, description="Per-build timeout, clamped to the deployment maximum."
    )
    machine_type: Optional[str] = Field(
        default=None, description="Build worker machine type (Cloud Build only), from the deployment's allowlist."
    )
    disk_size_gb: Optional[int] = Field(
        default=None, ge=1, description="Build worker disk size in GB (Cloud Build only), clamped."
    )
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

    @field_validator("context_uri")
    @classmethod
    def check_context_uri(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_context_uri(value)

    @field_validator("tag")
    @classmethod
    def check_tag(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_image_tag(value)

    @field_validator("build_args")
    @classmethod
    def check_build_args(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_build_arg_names(value, field="Build arg")

    @field_validator("secrets")
    @classmethod
    def check_secrets(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_secret_mapping(value)

    @model_validator(mode="after")
    def validate_destination(self) -> Self:
        if self.tag is not None and (self.registry is not None or self.version is not None):
            raise ValueError(
                "'tag' is a fully qualified destination and cannot be combined with 'registry' or 'version'"
            )
        return self

    @model_validator(mode="after")
    def validate_build_arg_namespaces(self) -> Self:
        # Plugin-declared secret_build_args are only known once to_spec() runs the pipeline, so the
        # three-way check lives on ImageBuildSpec; this catches the two-way case at definition time.
        check_build_arg_collisions(self.build_args, self.secrets, [])
        return self

    @model_validator(mode="after")
    def validate_base_definition(self) -> Self:
        """Enforce exactly-one-of ``base`` / ``base_dockerfile`` and reject unbuildable input.

        Everything checked here would otherwise surface as an opaque failure partway through a
        cluster build, minutes after submission, with the diagnostic buried in build logs.
        """
        if (self.base is None) == (self.base_dockerfile is None):
            raise ValueError(
                "Exactly one of 'base' (a registry reference) or 'base_dockerfile' (Dockerfile text) must be set"
            )
        if self.base_stage is not None and self.base_dockerfile is None:
            raise ValueError("'base_stage' selects a stage of 'base_dockerfile' and has no meaning alongside 'base'")

        if self.base_dockerfile is None:
            return self

        # Surfaces a missing FROM, an unknown base_stage, or a reserved stage name at definition
        # time. The result is recomputed in to_spec() rather than cached — the model is frozen, and
        # normalization is pure text work.
        normalize_base_dockerfile(self.base_dockerfile, self.base_stage)

        if references_auth_token(self.base_dockerfile):
            raise ValueError(
                f"'base_dockerfile' references the reserved build arg {AUTH_TOKEN_ARG}. The Cloud Build "
                "backend rewrites that name into a BuildKit secret mount on the generated stage, so a "
                "user-side occurrence would be rewritten into a stage where no secret is mounted. "
                "Rename it."
            )
        return self

    def _require_context_for_local_copies(self) -> None:
        """Reject a base Dockerfile that copies from a context when none is supplied.

        Checked when compiling rather than when validating, because ``with_context()`` is meant to
        be chainable: rejecting at construction would make ``from_dockerfile(text)`` unreachable for
        exactly the definitions that need a context. ``to_spec()`` still runs inside the build
        request, so the caller gets the error synchronously either way.
        """
        if self.base_dockerfile is None or self.context_uri is not None:
            return
        local = local_context_sources(self.base_dockerfile)
        if not local:
            return
        offenders = ", ".join(f"{item.instruction} {item.source} (line {item.line.number})" for item in local)
        raise ValueError(
            f"'base_dockerfile' copies from the build context but no 'context_uri' is set: {offenders}. "
            "Stage the context as an archive and pass its URI, or use COPY --from=<stage> / ADD <url>, "
            "which need no context."
        )

    @classmethod
    def from_base(cls, base: str | BaseImage, *, name: Optional[str] = None) -> Self:
        """Create an image from a base image reference or ``BaseImage`` enum value."""
        image = base.value if isinstance(base, BaseImage) else base
        return cls(base=image, name=name)

    @classmethod
    def from_dockerfile(
        cls,
        content: str,
        *,
        name: Optional[str] = None,
        base_stage: Optional[str] = None,
        context_uri: Optional[str] = None,
    ) -> Self:
        """Create an image whose base is compiled from ``content`` in the same build.

        Avoids the registry round-trip a custom base otherwise needs: the stages in ``content`` are
        merged ahead of the plugin stages, so one build produces the final image and nothing
        intermediate is pushed. ``base_stage`` selects which stage of a multi-stage ``content`` acts
        as the base (default: the last one).

        Pass ``context_uri`` when ``content`` copies from a build context.
        """
        return cls(base_dockerfile=content, name=name, base_stage=base_stage, context_uri=context_uri)

    @classmethod
    def from_dockerfile_path(
        cls,
        path: str | Path,
        *,
        name: Optional[str] = None,
        base_stage: Optional[str] = None,
        context_uri: Optional[str] = None,
    ) -> Self:
        """Create an image from a Dockerfile on disk, inlining its content immediately.

        The content is read **here**, at authoring time, and travels as text from then on: the
        orchestrator receives only ``yaml_content`` as a string and has no access to the caller's
        filesystem, so a path that survived into the request would be unresolvable.
        """
        return cls.from_dockerfile(
            Path(path).read_text(),
            name=name,
            base_stage=base_stage,
            context_uri=context_uri,
        )

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

    def with_context(self, context_uri: str) -> Self:
        """Return a copy that resolves ``COPY``/``ADD`` sources against a pre-staged context archive.

        ``context_uri`` names an archive the caller has already staged somewhere the build backend
        can read (e.g. ``gs://bucket/contexts/abc123.tar.gz``). Name the object by its content: the
        build tag is derived from the URI, not from the bytes the backend later fetches, so reusing
        one name for changed contents reads as an unchanged image.
        """
        return self.model_copy(update={"context_uri": validate_context_uri(context_uri)})

    def with_destination(
        self,
        *,
        tag: Optional[str] = None,
        registry: Optional[str] = None,
        version: Optional[str] = None,
    ) -> Self:
        """Return a copy pushing to a caller-chosen destination instead of the orchestrator's default.

        Pass either a fully qualified ``tag`` or a ``registry`` (combined with the image name and
        ``version``). The destination is checked against the deployment's registry allowlist at build
        time, so an orchestrator that has not opted in refuses it.

        Supplying ``version`` decouples the pushed tag from the content hash, which means the caller
        takes over deduplication — useful for a consumer maintaining its own content-addressed tags,
        and a footgun otherwise.
        """
        if tag is not None and (registry is not None or version is not None):
            raise ValueError(
                "'tag' is a fully qualified destination and cannot be combined with 'registry' or 'version'"
            )
        return self.model_copy(
            update={
                "tag": validate_image_tag(tag) if tag is not None else self.tag,
                "registry": registry if registry is not None else self.registry,
                "version": version if version is not None else self.version,
            }
        )

    def with_build_resources(
        self,
        *,
        timeout_seconds: Optional[int] = None,
        machine_type: Optional[str] = None,
        disk_size_gb: Optional[int] = None,
    ) -> Self:
        """Return a copy asking for more build capacity than the deployment's default.

        An inline base does not add builds, but each one now covers the base *and* the idegym layer,
        so a deployment sized for IdeGYM's own images can starve a multi-gigabyte environment image.
        Every value is clamped or allowlisted by the orchestrator, and none of them affect the image
        content, so none participate in the tag.

        ``machine_type`` and ``disk_size_gb`` are Cloud Build only; Kaniko's lever is the per-image
        ``resources`` field set by `with_runtime`.
        """
        # model_copy() bypasses field validation, so the positivity constraints the fields declare
        # are re-checked here; a negative timeout would otherwise reach the backend intact.
        for name, value in (("timeout_seconds", timeout_seconds), ("disk_size_gb", disk_size_gb)):
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1, got {value}")
        return self.model_copy(
            update={
                "timeout_seconds": timeout_seconds if timeout_seconds is not None else self.timeout_seconds,
                "machine_type": machine_type if machine_type is not None else self.machine_type,
                "disk_size_gb": disk_size_gb if disk_size_gb is not None else self.disk_size_gb,
            }
        )

    def with_build_args(self, **build_args: str) -> Self:
        """Return a copy with additional ``ARG`` values supplied to the build.

        Values are recorded in the image history, so this is for configuration, not credentials.
        Note that an ``ARG`` left unset is *unset* rather than empty, so a ``set -u`` script in the
        base Dockerfile needs ``${VAR:-}``.
        """
        merged = {**self.build_args, **build_args}
        validate_build_arg_names(merged, field="Build arg")
        check_build_arg_collisions(merged, self.secrets, [])
        return self.model_copy(update={"build_args": merged})

    def with_secrets(self, **secrets: str) -> Self:
        """Return a copy with additional build secrets, given as Secret Manager resource names.

        Only names travel in the definition; the value is resolved at build time. How it reaches the
        build depends on the backend: Cloud Build mounts it (``RUN --mount=type=secret,id=<id>``),
        while Kaniko has no mount mechanism and passes it as a build arg — which records it in the
        image history. A Dockerfile written against a mount is therefore Cloud-Build-only.
        """
        merged = {**self.secrets, **secrets}
        validate_secret_mapping(merged)
        check_build_arg_collisions(self.build_args, merged, [])
        return self.model_copy(update={"secrets": merged})

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

        Runs the plugin pipeline: for each plugin in order, ``apply()`` updates the
        ``BuildContext`` and then ``render()`` is called immediately with the updated context
        to produce a Dockerfile fragment. Each plugin's ``render()`` therefore sees only the
        context accumulated by itself and earlier plugins. The fragments and any
        ``run_commands`` are assembled into a complete Dockerfile.

        Build stages returned by ``get_build_stages()`` are prepended before the primary
        ``FROM`` instruction so plugins can compile artifacts in a separate stage and copy
        them into the final image via ``COPY --from=<stage>``.

        With a ``base_dockerfile``, the user's own stages are emitted ahead of the plugin stages and
        ``ctx.base`` becomes the alias of whichever stage acts as the base, so a ``FROM <alias>``
        inherits that stage's full image config (``ENV``, ``WORKDIR``, ``USER``, ``ENTRYPOINT``,
        ``CMD``) — the same thing publishing the base and referencing it by tag would have done.
        """
        self._require_context_for_local_copies()
        normalized = (
            normalize_base_dockerfile(self.base_dockerfile, self.base_stage)
            if self.base_dockerfile is not None
            else None
        )
        base_reference = normalized.alias if normalized is not None else self.base

        ctx = BuildContext(base=base_reference)
        build_stages: list[str] = []
        fragments: list[str] = []
        context_files: dict[str, bytes] = {}
        secret_build_args: list[str] = []
        for plugin in self.plugins:
            ctx = plugin.apply(ctx)
            for stage in plugin.get_build_stages(ctx):
                if stage.strip():
                    build_stages.append(stage.strip())
            fragment = plugin.render(ctx).strip()
            if fragment:
                fragments.append(fragment)
            mcp_fragment = _mcp_upstream_fragment(plugin, ctx)
            if mcp_fragment:
                fragments.append(mcp_fragment)
            for dest, resource in plugin.get_context_files(ctx).items():
                context_files[dest] = resource.read_bytes()
            for secret in plugin.get_build_secrets(ctx):
                if secret not in secret_build_args:
                    secret_build_args.append(secret)

        dockerfile_content = self._render_dockerfile(ctx, fragments, build_stages, normalized)
        return ImageBuildSpec(
            name=self.name,
            request=ctx.request,
            dockerfile_content=dockerfile_content,
            labels=dict(ctx.labels),
            context_path=ctx.context_path,
            context_files=context_files,
            platforms=list(self.platforms),
            runtime_class_name=self.runtime_class_name,
            resources=self.resources,
            secret_build_args=secret_build_args,
            context_uri=self.context_uri,
            build_args=dict(self.build_args),
            secrets=dict(self.secrets),
            tag=self.tag,
            registry=self.registry,
            version=self.version,
            timeout_seconds=self.timeout_seconds,
            machine_type=self.machine_type,
            disk_size_gb=self.disk_size_gb,
        )

    def _render_base_stage_header(self, base_reference: str) -> str:
        return "\n\n".join(
            [
                f"FROM {base_reference}",
                'SHELL ["/bin/bash", "-c"]',
                "USER root",
            ]
        )

    def _render_project_archive_env(self) -> str:
        return (
            "ARG IDEGYM_PROJECT_ARCHIVE_URL\n"
            "ARG IDEGYM_PROJECT_ARCHIVE_PATH\n"
            "ARG IDEGYM_AUTH_TOKEN\n"
            "ARG IDEGYM_AUTH_TYPE\n"
            "\n"
            'ENV IDEGYM_PROJECT_ARCHIVE_URL="$IDEGYM_PROJECT_ARCHIVE_URL"\n'
            'ENV IDEGYM_PROJECT_ARCHIVE_PATH="$IDEGYM_PROJECT_ARCHIVE_PATH"'
        )

    def _render_dockerfile(
        self,
        ctx: BuildContext,
        fragments: list[str],
        build_stages: Optional[list[str]] = None,
        normalized_base: Optional[NormalizedBase] = None,
    ) -> str:
        """Assemble the merged Dockerfile.

        Section order is ``[parser directives][base_dockerfile][plugin stages][idegym stage]``. The
        user's Dockerfile comes first so their pre-``FROM`` global ``ARG``s stay in scope for their
        own stages, and the plugin stages follow so they may reference the base alias. Parser
        directives are hoisted to the very top, the only place Docker still reads them — note that
        Kaniko ignores ``# syntax`` entirely.

        Everything from `_render_base_stage_header` onwards is identical whichever base form was
        used, which is what makes switching an existing definition to an inline base produce an
        equivalent image.
        """
        if build_stages is None:
            build_stages = []
        sections = [
            "\n".join(normalized_base.directives) if normalized_base is not None else "",
            normalized_base.body if normalized_base is not None else "",
            *build_stages,
            self._render_base_stage_header(ctx.base),
            self._render_project_archive_env() if ctx.request is not None else "",
            f'ENV IDEGYM_PROJECT_ROOT="{ctx.project_root}"',
            *fragments,
            f"USER {ctx.current_user}",
            _run_block(self.commands),
        ]

        return "\n\n".join(section for section in sections if section.strip()).strip() + "\n"

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
