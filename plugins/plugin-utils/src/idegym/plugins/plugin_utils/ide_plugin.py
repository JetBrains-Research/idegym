"""Shared image-build logic for the JetBrains IDE plugins (IDEA and PyCharm).

The two IDEs install almost identically: download and checksum a tarball from the
JetBrains CDN, pre-write IDE settings, optionally bake in the open-project plugin,
mcp-steroid and extra plugins, then at runtime bridge the loopback MCP port to
``0.0.0.0`` via socat. Everything that does not differ between them lives in
:class:`JetBrainsIdePlugin`; each concrete plugin only supplies its IDE-specific
metadata (package, install dir, script names) and ships its own ``resources/*.j2``
templates and prebuilt ``project-opener.zip``.
"""

import re
from importlib.resources import files
from importlib.resources.abc import Traversable
from shlex import quote
from typing import ClassVar, Optional

from idegym.api.plugin import BuildContext, PluginBase
from idegym.api.type import HttpUrl
from idegym.plugins.plugin_utils.assets import ide_context_files
from idegym.plugins.plugin_utils.external_plugins import (
    PluginSource,
    external_plugin_build_secrets,
    render_external_plugins,
    validate_zip_url,
)
from idegym.plugins.plugin_utils.ide_config import (
    render_entrypoint_install,
    render_first_run_config,
    render_mcp_config,
    render_open_project_config,
)
from idegym.plugins.plugin_utils.validators import check_linux_id
from jinja2 import BaseLoader, Environment
from pydantic import field_validator

# JetBrains IDE version, e.g. 2026.1 or 2026.1.1.
_VERSION_RE = re.compile(r"^\d{4}\.\d+(\.\d+)?$")
# mcp-steroid release version: a two- or three-part number followed by any number of
# lowercase alphanumeric suffix segments, e.g. 0.94.0-8682a5ce, 0.100-409f23a2 or
# 0.102.0-r-c68d8f15d. Upstream has changed the suffix shape more than once, so the segments
# are not constrained beyond being lowercase — which still rejects ``0.94.0-SNAPSHOT``.
_MCP_STEROID_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?(-[a-z0-9]+)*$")

MCP_STEROID_RELEASES = "https://github.com/jonnyzzz/mcp-steroid/releases/download"

# The bundled JetBrains MCP plugin binds loopback-only; the start script runs a socat
# bridge that re-listens on 0.0.0.0 so the server is reachable outside the container.
MCP_PORT = 64342
BRIDGE_PORT = 64343
# mcp-steroid uses its own loopback/bridge port pair.
MCP_STEROID_PORT = 6315
MCP_STEROID_BRIDGE_PORT = 6316
# All IDE settings are written here at build time and -Didea.config.path points at it, so
# the IDE never falls back to XDG detection (which needs $HOME, often unset in containers).
CONFIG_DIR = "/tmp/ide-config"


class JetBrainsIdePlugin(PluginBase):
    """Base class shared by the ``Idea`` and ``PyCharm`` image plugins.

    Handles everything common to both IDEs: version validation, the MCP upstream
    advertisement, staging build-context files, and rendering the Dockerfile from the
    per-IDE ``resources/*.j2`` templates. Concrete plugins set the ``_*`` class attributes
    and ship their own templates and prebuilt ``project-opener.zip``.

    Both bundle the JetBrains MCP server plugin (2026.1.1+), which binds to
    ``127.0.0.1:64342``; the start script bridges it to ``0.0.0.0:64343`` so it is
    reachable outside the container. When ``mcp_steroid=True`` the
    `mcp-steroid <https://github.com/jonnyzzz/mcp-steroid>`_ plugin is installed instead
    and advertised on port 6315.

    Attributes:
        version: IDE version in ``YYYY.N`` or ``YYYY.N.N`` format. Must be 2026.1.1 or newer.
        open_project: Install the open-project plugin and supervisord entry when a
            ``Project`` plugin precedes this one in the pipeline.
        mcp_steroid: Download and install the mcp-steroid plugin at build time. When
            ``True`` and ``open_project`` resolves to ``False``, the IDE starts without a
            project and agents open one via the ``open-project`` MCP tool.
        mcp_steroid_version: mcp-steroid version. Format ``X.Y`` or ``X.Y.Z``, optionally
            followed by lowercase alphanumeric suffix segments (e.g. ``0.94.0-8682a5ce`` or
            ``0.102.0-r-c68d8f15d``). Defaults to the latest tested. Ignored for the download
            itself when ``mcp_steroid_url`` is set.
        mcp_steroid_url: Explicit ``.zip`` download link, bypassing the URL built from
            ``mcp_steroid_version``. Set this when a release's tag does not follow the usual
            shape, so a new build can be pinned without a code change here.
        external_plugins: Extra plugins to bake into the bundled plugins dir, in order.
            Each :class:`PluginSource` names a ``.zip`` URL; set ``auth_env`` for downloads
            behind authentication. Installed after mcp-steroid.
        user: User to switch back to after installation. Defaults to ``ctx.current_user``.
    """

    # Per-IDE metadata, set by each concrete subclass.
    _PLUGIN_NAME: ClassVar[str]  # registry + server-plugin key ("idea" / "pycharm")
    _IDE_LABEL: ClassVar[str]  # human-facing name used in validation errors
    _PACKAGE: ClassVar[str]  # dotted package holding resources/ and build assets
    _CONTEXT_PREFIX: ClassVar[str]  # build-context COPY prefix for the per-IDE zip, e.g. "plugins/idea"
    # Open-project settings that differ by IDE: IDEA writes privacyPolicy.xml (PyCharm accepts the EUA
    # via vmoptions instead); PyCharm writes the extra PropertiesComponent other.xml.
    _OPEN_PROJECT_PRIVACY: ClassVar[bool]
    _OPEN_PROJECT_PROPERTIES: ClassVar[bool]
    # Both IDEs install into ${IDE_DIR} (pycharm's install template sets IDE_DIR=/opt/pycharm), so the
    # bundled plugins dir is the same for both.
    _PLUGINS_DIR: ClassVar[str] = "${IDE_DIR}/plugins"

    version: str = "2026.1.1"
    open_project: bool = True
    mcp_steroid: bool = False
    mcp_steroid_version: str = "0.102.0-r-c68d8f15d"
    mcp_steroid_url: Optional[HttpUrl] = None
    external_plugins: tuple[PluginSource, ...] = ()
    user: Optional[str] = None

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        if not _VERSION_RE.match(v):
            raise ValueError(f"Invalid {cls._IDE_LABEL} version: {v!r}. Expected format: YYYY.N or YYYY.N.N")
        return v

    @field_validator("mcp_steroid_version")
    @classmethod
    def _validate_mcp_steroid_version(cls, v: str) -> str:
        if not _MCP_STEROID_VERSION_RE.match(v):
            raise ValueError(
                f"Invalid mcp-steroid version: {v!r}. Expected format: X.Y or X.Y.Z, optionally followed by "
                "lowercase alphanumeric -suffix segments"
            )
        return v

    @field_validator("mcp_steroid_url")
    @classmethod
    def _validate_mcp_steroid_url(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else validate_zip_url(v, what="mcp_steroid_url")

    def mcp_steroid_release_tag(self) -> str:
        """The upstream release tag holding ``mcp_steroid_version``'s artifact.

        The tag is not simply ``v`` plus the version: ``v0.102`` ships
        ``mcp-steroid-0.102.0-r-c68d8f15d.zip`` while ``v0.94.0`` ships
        ``mcp-steroid-0.94.0-8682a5ce.zip``. Across every release so far the rule is the suffix
        shape — the multi-segment suffix upstream moved to in 0.102 comes with a two-part
        ``vMAJOR.MINOR`` tag, the older single-segment one with the full number. A release that
        breaks the rule again needs ``mcp_steroid_url``, not a patch here.
        """
        number, *suffix = self.mcp_steroid_version.split("-")
        if len(suffix) > 1:
            return "v" + ".".join(number.split(".")[:2])
        return f"v{number}"

    @field_validator("user")
    @classmethod
    def _validate_user(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            check_linux_id(v, "user")
        return v

    @staticmethod
    def _render_from(package: str, template_name: str, **kwargs: object) -> str:
        source = files(package).joinpath("resources").joinpath(template_name).read_text(encoding="utf-8")
        return Environment(loader=BaseLoader()).from_string(source).render(**kwargs).rstrip()

    @classmethod
    def _render(cls, template_name: str, **kwargs: object) -> str:
        """Render a per-IDE template (``install.j2``) from the concrete plugin's ``resources``."""
        return cls._render_from(cls._PACKAGE, template_name, **kwargs)

    @classmethod
    def _render_shared(cls, template_name: str, **kwargs: object) -> str:
        """Render a template shared by both IDEs, shipped once in plugin-utils ``resources``."""
        return cls._render_from(__package__, template_name, **kwargs)

    def _install_kwargs(self) -> dict[str, object]:
        """Extra kwargs for ``Dockerfile.install.j2`` beyond ``version`` / ``config_dir``.

        Overridden by ``Idea`` to pass ``headless``; PyCharm has no such option.
        """
        return {}

    def _writes_first_run_config(self) -> bool:
        """Whether to write the consent/disabled-plugins/What's-New settings at install time.

        PyCharm always runs against a display so it always needs them; ``Idea`` overrides this to
        write them only for virtual-display builds (headless IDEA starts none of those components).
        """
        return True

    def mcp_steroid_download_url(self) -> str:
        """Where the mcp-steroid ZIP is fetched from at build time.

        An explicit ``mcp_steroid_url`` wins; otherwise the link is built from
        ``mcp_steroid_version`` and the release tag it maps to.
        """
        if self.mcp_steroid_url is not None:
            return self.mcp_steroid_url
        return f"{MCP_STEROID_RELEASES}/{self.mcp_steroid_release_tag()}/mcp-steroid-{self.mcp_steroid_version}.zip"

    def get_build_secrets(self, ctx: BuildContext) -> list[str]:
        return external_plugin_build_secrets(self.external_plugins)

    def get_mcp_upstream(self, ctx: BuildContext) -> Optional[str]:
        if self.mcp_steroid:
            return f"http://localhost:{MCP_STEROID_PORT}/mcp"
        has_project = ctx.get_extra("idegym.has_project", False)
        if not (has_project and self.open_project):
            return None
        return f"http://localhost:{MCP_PORT}"

    def apply(self, ctx: BuildContext) -> BuildContext:
        existing = list(ctx.get_extra("idegym.enabled_server_plugins", []))
        if self._PLUGIN_NAME not in existing:
            existing.append(self._PLUGIN_NAME)
        return ctx.with_extra("idegym.enabled_server_plugins", existing)

    def get_context_files(self, ctx: BuildContext) -> dict[str, Traversable]:
        has_project = ctx.get_extra("idegym.has_project", False)
        return ide_context_files(
            self._PACKAGE,
            self._CONTEXT_PREFIX,
            install_open_project=has_project and self.open_project,
            mcp_steroid=self.mcp_steroid,
        )

    def render(self, ctx: BuildContext) -> str:
        user = self.user or ctx.current_user
        has_project = ctx.get_extra("idegym.has_project", False)
        install_plugin = has_project and self.open_project

        parts = [
            self._render("Dockerfile.install.j2", version=self.version, config_dir=CONFIG_DIR, **self._install_kwargs())
        ]

        # IDE settings files are generated (and XML-validated) in Python, shared across both IDEs.
        if self._writes_first_run_config():
            parts.append(render_first_run_config(CONFIG_DIR))

        if install_plugin:
            parts.append(render_mcp_config(CONFIG_DIR, mcp_port=MCP_PORT, bridge_port=BRIDGE_PORT))

        if self.mcp_steroid:
            parts.append(
                self._render_shared(
                    "Dockerfile.mcp_steroid.j2",
                    mcp_steroid_url=self.mcp_steroid_download_url(),
                    quoted_mcp_steroid_url=quote(self.mcp_steroid_download_url()),
                    plugins_dir=self._PLUGINS_DIR,
                )
            )

        if self.external_plugins:
            parts.append(render_external_plugins(self.external_plugins, plugins_dir=self._PLUGINS_DIR))

        if install_plugin:
            parts.append(
                self._render_shared(
                    "Dockerfile.open_project.j2",
                    context_prefix=self._CONTEXT_PREFIX,
                    mcp_port=MCP_PORT,
                    bridge_port=BRIDGE_PORT,
                )
            )
            parts.append(
                render_open_project_config(
                    CONFIG_DIR,
                    ctx.project_root,
                    privacy=self._OPEN_PROJECT_PRIVACY,
                    properties=self._OPEN_PROJECT_PROPERTIES,
                )
            )
            parts.append(render_entrypoint_install())
        elif self.mcp_steroid:
            # No project opener; start the IDE without a project so the agent can open one at
            # runtime using mcp-steroid's "open-project" MCP tool.
            parts.append(
                self._render_shared(
                    "Dockerfile.mcp_steroid_start.j2",
                    ide_name=self._IDE_LABEL,
                    mcp_steroid_port=MCP_STEROID_PORT,
                    mcp_steroid_bridge_port=MCP_STEROID_BRIDGE_PORT,
                )
            )
            parts.append(render_entrypoint_install())

        parts.append(f"\nUSER {user}")
        return "\n\n".join(parts)
