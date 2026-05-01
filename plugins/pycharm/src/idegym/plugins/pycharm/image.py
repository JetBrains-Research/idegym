import re
from textwrap import dedent
from typing import Optional

from idegym.api.plugin import BuildContext, PluginBase, image_plugin
from idegym.plugins.defaults.image import _check_linux_id
from pydantic import field_validator

_PYCHARM_VERSION_RE = re.compile(r"^\d{4}\.\d+(\.\d+)?$")

_MCP_PORT = 64342
_BRIDGE_PORT = 64343


@image_plugin("pycharm")
class PyCharm(PluginBase):
    """Install PyCharm Community with the JetBrains MCP server plugin.

    PyCharm CE does not support ``-Djava.awt.headless=true``, so ``start-pycharm.sh``
    starts Xvfb on ``:99`` to provide a virtual display before launching the IDE.

    **MCP server**: the JetBrains MCP plugin (``mcp_update_id``) binds to
    ``127.0.0.1:64342`` (loopback only). At runtime, ``start-pycharm.sh`` starts a
    socat bridge that re-listens on ``0.0.0.0:64343``, making the server reachable
    from outside the container. To use standalone::

        docker run -p 64343:64343 <image>

    then connect your MCP client to ``http://localhost:64343/mcp``.

    **Config path**: all IDE settings are written to ``/tmp/ide-config`` at build time,
    and ``-Didea.config.path=/tmp/ide-config`` is passed at startup. This avoids
    relying on XDG path detection in containers where ``$HOME`` may be unset.

    **Open-project plugin**: when the pipeline contains a ``Project`` plugin and
    ``open_project=True``, the pre-built plugin from
    ``plugins/pycharm/project-opener/project-opener.zip`` is installed into the
    bundled plugins directory (``${PYCHARM_DIR}/plugins/``) so PyCharm finds it before
    the ``open`` ``AppStarter`` command is dispatched. Requires build series 252+
    (PyCharm 2025.2+).

    Attributes:
        version: PyCharm version in ``YYYY.N`` or ``YYYY.N.N`` format.
        edition: ``"community"`` (default) or ``"professional"``.
        mcp_update_id: Marketplace update ID for the MCP server plugin. The default
            (``"882474"``) targets build series 252. Set to ``None`` to skip.
        open_project: Install the open-project plugin and supervisord entry when a
            ``Project`` plugin precedes this one in the pipeline.
        user: User to switch back to after installation. Defaults to ``ctx.current_user``.
    """

    version: str = "2025.3"
    edition: str = "community"
    mcp_update_id: Optional[str] = "882474"
    open_project: bool = True
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
        return f"http://localhost:{_MCP_PORT}"

    def apply(self, ctx: BuildContext) -> BuildContext:
        existing = list(ctx.get_extra("idegym.enabled_server_plugins", []))
        if "pycharm" not in existing:
            existing.append("pycharm")
        return ctx.with_extra("idegym.enabled_server_plugins", existing)

    def render(self, ctx: BuildContext) -> str:
        user = self.user or ctx.current_user
        base_url = "https://download.jetbrains.com/python"
        has_project = ctx.get_extra("idegym.has_project", False)
        install_plugin = has_project and self.open_project
        config_dir = "/tmp/ide-config"

        base = dedent(
            f"""\
            # Install PyCharm {self.edition} {self.version}
            USER root
            RUN set -eux; \\
                apt-get update -qq; \\
                apt-get install -y --no-install-recommends \\
                    ca-certificates curl unzip xvfb procps socat \\
                    libxtst6 libxrender1 libxi6 libfreetype6 fontconfig; \\
                apt-get clean; \\
                rm -rf /var/lib/apt/lists/*

            ENV PYCHARM_VERSION="{self.version}"
            ENV PYCHARM_DIR="/opt/pycharm"
            ENV IDE_CONFIG_PATH="{config_dir}"
            RUN set -eux; \\
                arch=$(dpkg --print-architecture); \\
                case "$arch" in \\
                    amd64) suffix="" ;; \\
                    arm64) suffix="-aarch64" ;; \\
                    *) echo "Unsupported arch: $arch" >&2; exit 1 ;; \\
                esac; \\
                archive="pycharm-{self.edition}-{self.version}${{suffix}}.tar.gz"; \\
                curl -fsSL "{base_url}/${{archive}}" -o /tmp/pycharm.tar.gz; \\
                curl -fsSL "{base_url}/${{archive}}.sha256" -o /tmp/pycharm.sha256; \\
                expected=$(cut -d' ' -f1 /tmp/pycharm.sha256); \\
                echo "$expected  /tmp/pycharm.tar.gz" | sha256sum -c -; \\
                mkdir -p ${{PYCHARM_DIR}}; \\
                tar -xzf /tmp/pycharm.tar.gz -C ${{PYCHARM_DIR}} --strip-components=1; \\
                rm /tmp/pycharm.tar.gz /tmp/pycharm.sha256

            ENV JAVA_HOME="${{PYCHARM_DIR}}/jbr"
            ENV PATH="${{JAVA_HOME}}/bin:${{PYCHARM_DIR}}/bin:${{PATH}}"
            ENV DISPLAY=":99"
            """
        ).rstrip()

        parts = [base]

        if self.mcp_update_id:
            mcp_block = dedent(
                f"""\

                # Install JetBrains MCP server plugin (updateId={self.mcp_update_id}, requires build 252+).
                # Plugin binds to 127.0.0.1:{_MCP_PORT}; start-pycharm.sh bridges to 0.0.0.0:{_BRIDGE_PORT}.
                RUN set -eux; \\
                    curl -fsSL "https://plugins.jetbrains.com/plugin/download?rel=true&updateId={self.mcp_update_id}" \\
                        -o /tmp/mcp-server.zip; \\
                    unzip -qo /tmp/mcp-server.zip -d "${{PYCHARM_DIR}}/plugins/"; \\
                    rm /tmp/mcp-server.zip

                # Enable MCP auto-start and disable Marketplace/SettingsSync to prevent
                # blocking modal dialogs on the Xvfb display at first run.
                RUN set -eux; \\
                    mkdir -p "{config_dir}/options"; \\
                    printf '%s\\n' \\
                        '<application>' \\
                        '  <component name="McpServerSettings">' \\
                        '    <option name="enableMcpServer" value="true" />' \\
                        '    <option name="enableBraveMode" value="true" />' \\
                        '  </component>' \\
                        '</application>' > "{config_dir}/options/mcpServer.xml"; \\
                    printf '%s\\n' 'com.intellij.marketplace' 'com.intellij.settingsSync' \\
                        > "{config_dir}/disabled_plugins.txt"
                """
            ).rstrip()
            parts.append(mcp_block)

        if install_plugin:
            plugin_block = dedent(
                f"""\

                # Install the open-project plugin into the bundled plugins dir so it is
                # present before the "open" AppStarter command is dispatched. Pre-built
                # ZIP checked into the repository to avoid a Gradle build stage.
                COPY plugins/pycharm/project-opener/project-opener.zip /tmp/open-project.zip
                RUN set -eux; \\
                    unzip -qo /tmp/open-project.zip -d "${{PYCHARM_DIR}}/plugins/" && rm /tmp/open-project.zip

                # Suppress first-run wizard, trust project path, accept EUA/consent.
                # PyCharm CE shows a Data Sharing dialog even with pre-accepted consent
                # files when the Marketplace plugin is active — disabled above via
                # disabled_plugins.txt.
                RUN set -eux; \\
                    mkdir -p "{config_dir}/options"; \\
                    printf '%s\\n' \\
                        '<application>' \\
                        '  <component name="PropertiesComponent">' \\
                        '    <property name="toolwindow.stripes.buttons.added" value="true" />' \\
                        '  </component>' \\
                        '</application>' > "{config_dir}/options/other.xml"; \\
                    printf '%s\\n' \\
                        '<application>' \\
                        '  <component name="GeneralSettings">' \\
                        '    <option name="showTipsOnStartup" value="false" />' \\
                        '  </component>' \\
                        '</application>' > "{config_dir}/options/ide.general.xml"; \\
                    printf '%s\\n' \\
                        '<application>' \\
                        '  <component name="Trusted.Paths.Settings">' \\
                        '    <option name="TRUSTED_PATHS">' \\
                        '      <list>' \\
                        '        <option value="{ctx.project_root}" />' \\
                        '      </list>' \\
                        '    </option>' \\
                        '    <option name="TRUSTED_PROJECT_LOCATORS">' \\
                        '      <list />' \\
                        '    </option>' \\
                        '  </component>' \\
                        '</application>' > "{config_dir}/options/trusted-paths.xml"; \\
                    mkdir -p "{ctx.home}/.local/share/JetBrains/consentOptions"; \\
                    printf '%s\\n' 'rsch.send.usage.stat:1.1:1:1700000000000' \\
                        > "{ctx.home}/.local/share/JetBrains/consentOptions/accepted"; \\
                    for f in "${{PYCHARM_DIR}}/bin/pycharm64.vmoptions" "${{PYCHARM_DIR}}/bin/pycharm.vmoptions"; do \\
                        [ -f "$f" ] && printf '\\n-Djb.privacy.policy.text=<!--999.999-->\\n-Dide.firstStartup=false\\n-Dide.no.platform.update=true\\n-Dide.trust.all.projects=true\\n-Djb.consents.confirmation.enabled=false\\n-Dide.show.tips.on.startup.default.value=false\\n-Dide.browser.jcef.enabled=false\\n-XX:-UsePerfData\\n-XX:+PerfDisableSharedMem\\n-XX:-UseLargePages\\n-Xshare:off\\n-XX:+UseContainerSupport\\n-XX:ErrorFile=/tmp/jvm-crash.log\\n' >> "$f" || true; \\
                    done

                # start-pycharm.sh: starts Xvfb on :99 (PyCharm CE requires a display),
                # launches PyCharm, waits for MCP on {_MCP_PORT}, bridges 0.0.0.0:{_BRIDGE_PORT} → 127.0.0.1:{_MCP_PORT}.
                COPY plugins/pycharm/scripts/start-pycharm.sh /usr/local/bin/start-pycharm.sh
                RUN chmod +x /usr/local/bin/start-pycharm.sh
                COPY plugins/pycharm/scripts/supervisord-pycharm.conf /etc/supervisor/conf.d/pycharm.conf
                """
            ).rstrip()
            parts.append(plugin_block)

        parts.append(f"\nUSER {user}")
        return "\n".join(parts)
