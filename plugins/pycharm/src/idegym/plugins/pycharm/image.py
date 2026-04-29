import re
from textwrap import dedent
from typing import Optional

from idegym.api.plugin import BuildContext, PluginBase, image_plugin
from idegym.plugins.defaults.image import _check_linux_id
from pydantic import field_validator

# PyCharm version: YYYY.N or YYYY.N.N
_PYCHARM_VERSION_RE = re.compile(r"^\d{4}\.\d+(\.\d+)?$")


@image_plugin("pycharm")
class PyCharm(PluginBase):
    """Install PyCharm IDE and Java (via SDKMAN) into the image.

    Installs dependencies, downloads and extracts PyCharm, then switches back to the
    active user. The ``USER root`` / ``USER <user>`` framing means this plugin can be
    placed anywhere in the pipeline regardless of the current user.

    Also adds ``"pycharm"`` to ``ctx.extras["idegym.enabled_server_plugins"]`` so that
    ``IdeGYMServer`` writes it to ``/etc/idegym/plugins.json`` at build time, enabling
    the ``PyCharmPlugin`` server endpoint at runtime.

    Attributes:
        version: PyCharm version string in ``YYYY.N`` or ``YYYY.N.N`` format.
        edition: ``"professional"`` (default) or ``"community"``.
        user: Target user to switch back to after installation. Defaults to ``ctx.current_user``.
    """

    version: str = "2025.3"
    edition: str = "professional"
    user: Optional[str] = None

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        if not _PYCHARM_VERSION_RE.match(v):
            raise ValueError(f"Invalid PyCharm version: {v!r}. Expected format: YYYY.N or YYYY.N.N")
        return v

    @field_validator("edition")
    @classmethod
    def _validate_edition(cls, v: str) -> str:
        if v not in ("professional", "community"):
            raise ValueError(f"Invalid PyCharm edition: {v!r}. Must be 'professional' or 'community'.")
        return v

    @field_validator("user")
    @classmethod
    def _validate_user(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _check_linux_id(v, "user")
        return v

    @classmethod
    def get_mcp_upstream(cls) -> Optional[str]:
        """PyCharm Professional exposes an MCP server on port 6789."""
        return "http://localhost:6789/mcp"

    def apply(self, ctx: BuildContext) -> BuildContext:
        existing = list(ctx.get_extra("idegym.enabled_server_plugins", []))
        if "pycharm" not in existing:
            existing.append("pycharm")
        return ctx.with_extra("idegym.enabled_server_plugins", existing)

    def render(self, ctx: BuildContext) -> str:
        user = self.user or ctx.current_user
        return dedent(
            f"""\
            # Install PyCharm {self.edition} {self.version}
            USER root
            RUN set -eux; \\
                apt-get update -qq; \\
                apt-get install -y --no-install-recommends \\
                    wget curl zip unzip \\
                    libxtst6 libxrender1 libxi6 libfreetype6 fontconfig; \\
                apt-get clean; \\
                rm -rf /var/lib/apt/lists/*

            # Install Java via SDKMAN (required for PyCharm)
            RUN curl -s "https://get.sdkman.io" | bash && \\
                bash -c "source /root/.sdkman/bin/sdkman-init.sh && sdk install java 21.0.5-tem"

            ENV JAVA_HOME="/root/.sdkman/candidates/java/current"
            ENV PATH="${{JAVA_HOME}}/bin:${{PATH}}"

            # Download and install PyCharm
            ENV PYCHARM_VERSION="{self.version}"
            ENV PYCHARM_DIR="/opt/pycharm"
            RUN wget -q "https://download.jetbrains.com/python/pycharm-{self.edition}-${{PYCHARM_VERSION}}.tar.gz" \\
                    -O /tmp/pycharm.tar.gz && \\
                mkdir -p ${{PYCHARM_DIR}} && \\
                tar -xzf /tmp/pycharm.tar.gz -C ${{PYCHARM_DIR}} --strip-components=1 && \\
                rm /tmp/pycharm.tar.gz

            ENV PATH="${{PYCHARM_DIR}}/bin:${{PATH}}"
            ENV DISPLAY=":99"

            USER {user}
            """
        ).strip()
