import re
from importlib.resources import files
from typing import Optional

from idegym.api.plugin import BuildContext, PluginBase, image_plugin
from idegym.plugins.defaults.image import _check_linux_id
from jinja2 import BaseLoader, Environment
from pydantic import field_validator

_IDEA_VERSION_RE = re.compile(r"^\d{4}\.\d+(\.\d+)?$")

_MCP_PORT = 64342
_BRIDGE_PORT = 64343
_CONFIG_DIR = "/tmp/ide-config"


def _render(template_name: str, **kwargs: object) -> str:
    source = files(__package__).joinpath("resources").joinpath(template_name).read_text(encoding="utf-8")
    return Environment(loader=BaseLoader()).from_string(source).render(**kwargs).rstrip()


@image_plugin("idea")
class Idea(PluginBase):
    """Install IntelliJ IDEA with the JetBrains MCP server plugin.

    Requires IDEA 2026.1.1 or newer. Older versions are not supported.

    IDEA supports ``-Djava.awt.headless=true`` natively, so no display
    server is needed — it starts faster and uses less memory than PyCharm.

    **MCP server**: the JetBrains MCP plugin is bundled in 2026.1.1+ and binds to
    ``127.0.0.1:64342`` (loopback only). Plugin versions are listed at
    https://plugins.jetbrains.com/plugin/26071-mcp-server/versions. At runtime,
    ``start-idea.sh`` starts a socat bridge that re-listens on ``0.0.0.0:64343``,
    making the server reachable from outside the container. To use standalone::

        docker run -p 64343:64343 <image>

    then connect your MCP client to ``http://localhost:64343/mcp``.

    **Config path**: all IDE settings are written to ``/tmp/ide-config`` at build time,
    and ``-Didea.config.path=/tmp/ide-config`` is passed at startup. This avoids
    relying on XDG path detection in containers where ``$HOME`` may be unset.

    **Open-project plugin**: when the pipeline contains a ``Project`` plugin and
    ``open_project=True``, the pre-built plugin from
    ``plugins/idea/project-opener/project-opener.zip`` is installed into the bundled
    plugins directory (``${IDE_DIR}/plugins/``) so IDEA finds it before the ``open``
    ``AppStarter`` command is dispatched. Requires build series 261+
    (IDEA 2026.1+).

    Attributes:
        version: IDEA version in ``YYYY.N`` or ``YYYY.N.N`` format. Must be 2026.1.1
            or newer; older versions are not supported.
        open_project: Install the open-project plugin and supervisord entry when a
            ``Project`` plugin precedes this one in the pipeline.
        user: User to switch back to after installation. Defaults to ``ctx.current_user``.
    """

    version: str = "2026.1.1"
    open_project: bool = True
    user: Optional[str] = None

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        if not _IDEA_VERSION_RE.match(v):
            raise ValueError(f"Invalid IDEA version: {v!r}. Expected format: YYYY.N or YYYY.N.N")
        return v

    @field_validator("user")
    @classmethod
    def _validate_user(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _check_linux_id(v, "user")
        return v

    def get_mcp_upstream(self, ctx: BuildContext) -> Optional[str]:
        has_project = ctx.get_extra("idegym.has_project", False)
        if not (has_project and self.open_project):
            return None
        return f"http://localhost:{_BRIDGE_PORT}"

    def apply(self, ctx: BuildContext) -> BuildContext:
        has_project = ctx.get_extra("idegym.has_project", False)
        if has_project and self.open_project:
            existing = list(ctx.get_extra("idegym.enabled_server_plugins", []))
            if "idea" not in existing:
                existing.append("idea")
            ctx = ctx.with_extra("idegym.enabled_server_plugins", existing)
        return ctx

    def render(self, ctx: BuildContext) -> str:
        user = self.user or ctx.current_user
        has_project = ctx.get_extra("idegym.has_project", False)
        install_plugin = has_project and self.open_project

        parts = [_render("Dockerfile.install.j2", version=self.version, config_dir=_CONFIG_DIR)]

        if install_plugin:
            parts.append(
                _render(
                    "Dockerfile.mcp.j2",
                    config_dir=_CONFIG_DIR,
                )
            )

        if install_plugin:
            parts.append(
                _render(
                    "Dockerfile.open_project.j2",
                    config_dir=_CONFIG_DIR,
                    project_root=ctx.project_root,
                    mcp_port=_MCP_PORT,
                    bridge_port=_BRIDGE_PORT,
                )
            )

        parts.append(f"\nUSER {user}")
        return "\n\n".join(parts)
