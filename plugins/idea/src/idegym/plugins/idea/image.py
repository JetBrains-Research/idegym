import re
from textwrap import dedent
from typing import Optional

from idegym.api.plugin import BuildContext, PluginBase, image_plugin
from idegym.plugins.defaults.image import _check_linux_id
from pydantic import field_validator

_IDEA_VERSION_RE = re.compile(r"^\d{4}\.\d+(\.\d+)?$")

_MCP_PORT = 64342
_BRIDGE_PORT = 64343


@image_plugin("idea")
class Idea(PluginBase):
    """Install IntelliJ IDEA Community with the JetBrains MCP server plugin.

    IDEA Community supports ``-Djava.awt.headless=true`` natively, so no display
    server is needed — it starts faster and uses less memory than PyCharm CE.

    **MCP server**: the JetBrains MCP plugin (``mcp_update_id``) binds to
    ``127.0.0.1:64342`` (loopback only). At runtime, ``start-idea.sh`` starts a socat
    bridge that re-listens on ``0.0.0.0:64343``, making the server reachable from
    outside the container. To use standalone::

        docker run -p 64343:64343 <image>

    then connect your MCP client to ``http://localhost:64343/mcp``.

    **Config path**: all IDE settings are written to ``/tmp/ide-config`` at build time,
    and ``-Didea.config.path=/tmp/ide-config`` is passed at startup. This avoids
    relying on XDG path detection in containers where ``$HOME`` may be unset.

    **Open-project plugin**: when the pipeline contains a ``Project`` plugin and
    ``open_project=True``, the pre-built plugin from
    ``plugins/idea/project-opener/project-opener.zip`` is installed into the bundled
    plugins directory (``${IDE_DIR}/plugins/``) so IDEA finds it before the ``open``
    ``AppStarter`` command is dispatched. Requires build series 252+ (IDEA 2025.2+).

    Attributes:
        version: IDEA version in ``YYYY.N`` or ``YYYY.N.N`` format.
        mcp_update_id: Marketplace update ID for the MCP server plugin. The default
            (``"882474"``) targets build series 252. Set to ``None`` to skip.
        open_project: Install the open-project plugin and supervisord entry when a
            ``Project`` plugin precedes this one in the pipeline.
        user: User to switch back to after installation. Defaults to ``ctx.current_user``.
    """

    version: str = "2025.3"
    mcp_update_id: Optional[str] = "882474"
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

    @classmethod
    def get_mcp_upstream(cls) -> Optional[str]:
        return f"http://localhost:{_MCP_PORT}"

    def apply(self, ctx: BuildContext) -> BuildContext:
        existing = list(ctx.get_extra("idegym.enabled_server_plugins", []))
        if "idea" not in existing:
            existing.append("idea")
        return ctx.with_extra("idegym.enabled_server_plugins", existing)

    def render(self, ctx: BuildContext) -> str:
        user = self.user or ctx.current_user
        base_url = "https://download.jetbrains.com/idea"
        has_project = ctx.get_extra("idegym.has_project", False)
        install_plugin = has_project and self.open_project
        config_dir = "/tmp/ide-config"

        base = dedent(
            f"""\
            # Install IntelliJ IDEA Community {self.version}
            USER root
            RUN set -eux; \\
                apt-get update -qq; \\
                apt-get install -y --no-install-recommends \\
                    ca-certificates curl unzip procps socat \\
                    libx11-6 libxext6 libxrender1 libxtst6 libxi6 libxrandr2 \\
                    libfreetype6 libfontconfig1; \\
                apt-get clean; \\
                rm -rf /var/lib/apt/lists/*

            ENV IDEA_VERSION="{self.version}"
            ENV IDE_DIR="/opt/idea"
            ENV IDE_CONFIG_PATH="{config_dir}"
            RUN set -eux; \\
                arch=$(dpkg --print-architecture); \\
                case "$arch" in \\
                    amd64) suffix="" ;; \\
                    arm64) suffix="-aarch64" ;; \\
                    *) echo "Unsupported arch: $arch" >&2; exit 1 ;; \\
                esac; \\
                archive="ideaIC-{self.version}${{suffix}}.tar.gz"; \\
                curl -fsSL "{base_url}/${{archive}}" -o /tmp/idea.tar.gz; \\
                curl -fsSL "{base_url}/${{archive}}.sha256" -o /tmp/idea.sha256; \\
                expected=$(cut -d' ' -f1 /tmp/idea.sha256); \\
                echo "$expected  /tmp/idea.tar.gz" | sha256sum -c -; \\
                mkdir -p ${{IDE_DIR}}; \\
                tar -xzf /tmp/idea.tar.gz -C ${{IDE_DIR}} --strip-components=1; \\
                rm /tmp/idea.tar.gz /tmp/idea.sha256

            ENV JAVA_HOME="${{IDE_DIR}}/jbr"
            ENV PATH="${{JAVA_HOME}}/bin:${{IDE_DIR}}/bin:${{PATH}}"

            # Headless + startup-suppression flags in vmoptions (applies without CLI args).
            # IDEA CE supports java.awt.headless=true natively; no Xvfb required.
            RUN for f in "${{IDE_DIR}}/bin/idea64.vmoptions" "${{IDE_DIR}}/bin/idea.vmoptions"; do \\
                    [ -f "$f" ] && printf '\\n-Djava.awt.headless=true\\n-Didea.trust.all.projects=true\\n-Djb.consents.confirmation.enabled=false\\n-Dide.show.tips.on.startup.default.value=false\\n-Dide.no.platform.update=true\\n-Dide.browser.jcef.enabled=false\\n-XX:-UsePerfData\\n-XX:+PerfDisableSharedMem\\n-XX:+UseContainerSupport\\n-XX:ErrorFile=/tmp/jvm-crash.log\\n' >> "$f" || true; \\
                done
            """
        ).rstrip()

        parts = [base]

        if self.mcp_update_id:
            mcp_block = dedent(
                f"""\

                # Install JetBrains MCP server plugin (updateId={self.mcp_update_id}, requires build 252+).
                RUN set -eux; \\
                    curl -fsSL "https://plugins.jetbrains.com/plugin/download?rel=true&updateId={self.mcp_update_id}" \\
                        -o /tmp/mcp-server.zip; \\
                    unzip -qo /tmp/mcp-server.zip -d "${{IDE_DIR}}/plugins/"; \\
                    rm /tmp/mcp-server.zip

                # Enable MCP auto-start at IDE_CONFIG_PATH so -Didea.config.path in
                # start-idea.sh points directly to it (avoids XDG detection in containers).
                RUN set -eux; \\
                    mkdir -p "{config_dir}/options"; \\
                    printf '%s\\n' \\
                        '<application>' \\
                        '  <component name="McpServerSettings">' \\
                        '    <option name="enableMcpServer" value="true" />' \\
                        '    <option name="enableBraveMode" value="true" />' \\
                        '  </component>' \\
                        '</application>' > "{config_dir}/options/mcpServer.xml"
                """
            ).rstrip()
            parts.append(mcp_block)

        if install_plugin:
            plugin_block = dedent(
                f"""\

                # Install the open-project plugin into the bundled plugins dir so it is
                # present before the "open" AppStarter command is dispatched. Pre-built
                # ZIP checked into the repository to avoid a Gradle build stage.
                COPY plugins/idea/project-opener/project-opener.zip /tmp/open-project.zip
                RUN set -eux; \\
                    unzip -qo /tmp/open-project.zip -d "${{IDE_DIR}}/plugins/" && rm /tmp/open-project.zip

                # Suppress first-run wizard, trust project path, accept EUA.
                RUN set -eux; \\
                    mkdir -p "{config_dir}/options"; \\
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
                    printf '%s\\n' \\
                        '<application>' \\
                        '  <component name="PrivacyPolicyAgreement">' \\
                        '    <option name="versionOfAccepted" value="999.999"/>' \\
                        '  </component>' \\
                        '</application>' > "{config_dir}/options/privacyPolicy.xml"

                # start-idea.sh: launches IDEA headless, waits for MCP on {_MCP_PORT},
                # then bridges 0.0.0.0:{_BRIDGE_PORT} → 127.0.0.1:{_MCP_PORT} via socat.
                COPY plugins/idea/scripts/start-idea.sh /usr/local/bin/start-idea.sh
                RUN chmod +x /usr/local/bin/start-idea.sh
                COPY plugins/idea/scripts/supervisord-idea.conf /etc/supervisor/conf.d/idea.conf
                """
            ).rstrip()
            parts.append(plugin_block)

        parts.append(f"\nUSER {user}")
        return "\n".join(parts)
