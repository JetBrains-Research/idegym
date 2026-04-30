import re
from shlex import quote
from textwrap import dedent
from typing import Optional

from idegym.api.plugin import BuildContext, PluginBase, image_plugin
from idegym.plugins.defaults.image import _check_linux_id
from pydantic import field_validator

# PyCharm version: YYYY.N or YYYY.N.N
_PYCHARM_VERSION_RE = re.compile(r"^\d{4}\.\d+(\.\d+)?$")

# Lines of the supervisord program config for PyCharm.
# %(ENV_IDEGYM_PROJECT_ROOT)s is a supervisord variable expansion — it is resolved
# at container start time, not at image build time.
_SUPERVISOR_CONF_LINES = [
    "[program:pycharm]",
    "command=/usr/local/bin/start-pycharm.sh",
    "priority=10",
    "autostart=true",
    "autorestart=false",
    # startsecs=0: supervisord considers the program RUNNING immediately.
    # Without this, a crash before 10 s is classified as a startup failure,
    # which triggers startretries regardless of autorestart=false.
    "startsecs=0",
    # startretries=0: do not retry startup failures at all.
    "startretries=0",
    "stdout_logfile=/dev/stdout",
    "stdout_logfile_maxbytes=0",
    "stderr_logfile=/dev/stderr",
    "stderr_logfile_maxbytes=0",
    "redirect_stderr=false",
]

# Wrapper script written into the image at /usr/local/bin/start-pycharm.sh.
# Starts Xvfb + openbox, dismisses the "Data Sharing" consent dialog via xdotool
# (PyCharm 2024.x shows it even when the consent file is pre-created), then
# exec-s PyCharm so supervisord sees the correct PID.
#
# Dialog dismissal strategy (validated empirically on PyCharm 2024.3 / 1024x768):
#   - Dialog appears ~30-50s into startup; poll every 3s from t=30s onward.
#   - Java AWT rejects XSendEvent-based synthetic keyboard events; XTEST mouse
#     events (xdotool click) DO work.
#   - The primary button sits at ~89% of the dialog height; scan x=20..80% and
#     click until the window disappears.
_START_PYCHARM_SCRIPT = """\
#!/usr/bin/env bash
set -euo pipefail

# Kill any existing Xvfb on display :99 and remove all its leftover files.
# The process itself must be killed first; removing only the lock file leaves
# the socket file (/tmp/.X11-unix/X99) which blocks a fresh Xvfb start.
pkill -x Xvfb 2>/dev/null || true
sleep 0.2
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
echo ">>> Starting Xvfb on :99"
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
sleep 1
echo ">>> Starting openbox window manager"
pkill -x openbox 2>/dev/null || true
DISPLAY=:99 openbox --sm-disable &
sleep 1

echo ">>> Launching PyCharm (no CLI arg — open-project plugin opens IDEGYM_PROJECT_ROOT)"
# Start without CLI arg so the Welcome Screen shows; AppLifecycleListener.appStarted()
# fires only after the Welcome Screen is shown, not during splash-screen startup.
pycharm.sh >/tmp/pycharm-stdout.log 2>&1 &
PYCHARM_PID=$!

# Dismiss the "Data Sharing" consent dialog in the background.
# The dialog blocks the EDT (and therefore appStarted()) until dismissed.
# We scan horizontally across the button row at 89% of the dialog height.
(sleep 30; for _i in $(seq 1 60); do
    wid=$(DISPLAY=:99 xdotool search --name "Data Sharing" 2>/dev/null | head -1)
    if [ -n "$wid" ]; then
        echo ">>> Found Data Sharing dialog (window $wid), clicking to dismiss..."
        geo=$(DISPLAY=:99 xdotool getwindowgeometry "$wid" 2>/dev/null)
        pos_x=$(echo "$geo" | awk '/Position/ {split($2, a, ","); print int(a[1])}')
        pos_y=$(echo "$geo" | awk '/Position/ {split($2, a, ","); gsub(/[^0-9].*/, "", a[2]); print int(a[2])}')
        geo_w=$(echo "$geo" | awk '/Geometry/ {split($2, a, "x"); print int(a[1])}')
        geo_h=$(echo "$geo" | awk '/Geometry/ {split($2, a, "x"); print int(a[2])}')
        btn_y=$(( pos_y + geo_h * 89 / 100 ))
        for pct in 20 30 40 50 60 70 80; do
            btn_x=$(( pos_x + geo_w * pct / 100 ))
            DISPLAY=:99 xdotool mousemove "$btn_x" "$btn_y"
            sleep 0.05
            DISPLAY=:99 xdotool click 1
            sleep 0.15
            still=$(DISPLAY=:99 xdotool search --name "Data Sharing" 2>/dev/null | head -1)
            if [ -z "$still" ]; then
                echo ">>> Dialog dismissed by click at ${btn_x},${btn_y} (${pct}%)"
                break 2
            fi
        done
    fi
    sleep 3
done) &

wait "$PYCHARM_PID" || {
    ec=$?
    echo ">>> PyCharm exited with code $ec"
    echo "=== JVM crash report ==="
    # -XX:ErrorFile=/tmp/jvm-crash.log is set in vmoptions; fall back to default pattern.
    for f in /tmp/jvm-crash.log /tmp/hs_err_pid*.log; do
        [ -f "$f" ] && { echo "Crash file: $f"; cat "$f"; break; }
    done
    echo "=== pycharm.sh stdout/stderr ==="
    cat /tmp/pycharm-stdout.log 2>/dev/null || echo "(empty)"
}
"""


def _write_supervisor_conf() -> str:
    """Return a Dockerfile RUN fragment that writes the PyCharm supervisord config.

    Uses ``printf '%b'`` so that the ``\\n`` separators in the quoted argument are
    expanded to real newlines by the shell. The config lines themselves contain no
    single-quote characters, so shell-quoting with ``shlex.quote`` is safe.
    """
    conf_arg = "\\n".join(_SUPERVISOR_CONF_LINES) + "\\n"
    return (
        "RUN set -eux; \\\n"
        "    mkdir -p /etc/supervisor/conf.d; \\\n"
        f"    printf '%b' {quote(conf_arg)} > /etc/supervisor/conf.d/pycharm.conf"
    )


def _write_start_script() -> str:
    """Return a Dockerfile RUN fragment that writes the start-pycharm.sh wrapper script.

    Real newlines are replaced with the two-character sequence ``\\n`` before quoting
    so that Docker's Dockerfile parser does not treat them as new instructions.
    ``printf '%b'`` then expands the ``\\n`` sequences back to real newlines when
    writing the file — the same approach used by ``_write_supervisor_conf()``.
    """
    script_arg = _START_PYCHARM_SCRIPT.replace("\n", "\\n")
    quoted = quote(script_arg)
    return (
        "RUN set -eux; \\\n"
        f"    printf '%b' {quoted} > /usr/local/bin/start-pycharm.sh; \\\n"
        "    chmod +x /usr/local/bin/start-pycharm.sh"
    )


@image_plugin("pycharm")
class PyCharm(PluginBase):
    """Install PyCharm IDE using its bundled JetBrains Runtime (JBR) into the image.

    Installs dependencies, downloads and extracts PyCharm, then switches back to the
    active user. The ``USER root`` / ``USER <user>`` framing means this plugin can be
    placed anywhere in the pipeline regardless of the current user.

    Also adds ``"pycharm"`` to ``ctx.extras[\"idegym.enabled_server_plugins\"]`` so that
    ``IdeGYMServer`` writes it to ``/etc/idegym/plugins.json`` at build time, enabling
    the ``PyCharmPlugin`` server endpoint at runtime.

    When the build context contains a project (i.e. a ``Project`` plugin was applied
    earlier in the pipeline), this plugin also:

    * compiles the ``open-project`` JetBrains plugin in a separate Gradle build stage
      and installs it into the user plugin directory, so the IDE opens
      ``$IDEGYM_PROJECT_ROOT`` automatically at startup; and
    * pre-creates the required PyCharm config files to suppress first-run wizard,
      accept the EUA/privacy policy, and trust the project directory;
    * writes ``/usr/local/bin/start-pycharm.sh`` — a wrapper that starts Xvfb,
      openbox, a background dialog-dismissal loop (for the "Data Sharing" modal),
      and then PyCharm itself; and
    * writes a supervisord program config to ``/etc/supervisor/conf.d/pycharm.conf``
      so that supervisord starts the wrapper when the container starts.

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

    def get_build_stages(self, ctx: BuildContext) -> list[str]:
        """Return a Gradle build stage for the open-project plugin when a project is present.

        The stage compiles ``jetbrains-plugin/open-project/`` (relative to the Docker build
        context root) using a Gradle + JDK 17 image. The resulting plugin zip is later
        consumed by ``render()`` via ``COPY --from=pycharm-plugin-builder``.

        The stage is only emitted when a ``Project`` plugin was applied earlier in the
        pipeline (i.e. ``ctx.get_extra("idegym.has_project")`` is ``True``).
        """
        if not ctx.get_extra("idegym.has_project", False):
            return []
        return [
            dedent("""\
                FROM gradle:8.14-jdk17 AS pycharm-plugin-builder
                WORKDIR /build
                COPY jetbrains-plugin/open-project/ ./
                RUN gradle buildPlugin --no-daemon -q
            """).strip()
        ]

    def render(self, ctx: BuildContext) -> str:
        user = self.user or ctx.current_user
        base_url = "https://download.jetbrains.com/python"
        has_project = ctx.get_extra("idegym.has_project", False)

        base = dedent(
            f"""\
            # Install PyCharm {self.edition} {self.version}
            USER root
            RUN set -eux; \\
                apt-get update -qq; \\
                apt-get install -y --no-install-recommends \\
                    ca-certificates curl unzip xvfb procps openbox xdotool \\
                    libxtst6 libxrender1 libxi6 libfreetype6 fontconfig; \\
                apt-get clean; \\
                rm -rf /var/lib/apt/lists/*

            # Download, verify checksum, and extract PyCharm.
            # Architecture is detected at build time: amd64 uses the default archive;
            # arm64 uses the -aarch64 variant published by JetBrains.
            # PyCharm 2022+ bundles JBR at $PYCHARM_DIR/jbr — no external JDK needed.
            ENV PYCHARM_VERSION="{self.version}"
            ENV PYCHARM_DIR="/opt/pycharm"
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

        if has_project:
            edition_dir = "PyCharmCE" if self.edition == "community" else "PyCharm"
            # idea.plugins.path IS the plugin root — plugins go directly in it.
            # PyCharm scans ~/.local/share/JetBrains/PyCharm[CE]<version>/ for user plugins;
            # installing to ${PYCHARM_DIR}/plugins/ does NOT work for custom plugins in 2024.x.
            user_plugins_dir = f"{ctx.home}/.local/share/JetBrains/{edition_dir}{self.version}"
            config_dir = f"{ctx.home}/.config/JetBrains/{edition_dir}{self.version}"

            install_plugin = dedent(
                f"""\

                # Install open-project plugin — opens $IDEGYM_PROJECT_ROOT at IDE startup.
                COPY --from=pycharm-plugin-builder /build/build/distributions/open-project-*.zip /tmp/open-project.zip
                RUN set -eux; \\
                    mkdir -p "{user_plugins_dir}"; \\
                    cd "{user_plugins_dir}" && unzip /tmp/open-project.zip && rm /tmp/open-project.zip

                # Pre-create PyCharm config to suppress first-run wizard, trust the project,
                # and accept the EUA/privacy policy so no blocking dialogs appear at startup.
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
                        [ -f "$f" ] && printf '\\n-Djb.privacy.policy.text=<!--999.999-->\\n-Dide.firstStartup=false\\n-Dide.no.platform.update=true\\n-Dide.trust.all.projects=true\\n-Dide.browser.jcef.enabled=false\\n-XX:-UsePerfData\\n-XX:+PerfDisableSharedMem\\n-XX:-UseLargePages\\n-Xshare:off\\n-XX:+UseContainerSupport\\n-XX:ErrorFile=/tmp/jvm-crash.log\\n' >> "$f" || true; \\
                    done

                # Register PyCharm as a supervisord-managed process.
                """
            ).rstrip()
            parts.append(install_plugin)
            parts.append(_write_start_script())
            parts.append(_write_supervisor_conf())

        parts.append(f"\nUSER {user}")
        return "\n".join(parts)
