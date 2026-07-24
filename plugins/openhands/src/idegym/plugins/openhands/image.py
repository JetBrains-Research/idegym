"""IdeGYM image plugin that installs and starts the agentless OpenHands Tools Service.

It installs the pinned OpenHands packages plus this plugin's runtime, provisions the
state/output/log/tmux directories owned by the project user, installs a supervisor entry that
starts the loopback service, declares the loopback MCP upstream, and enables the ``openhands``
server plugin. Order it after the user/project plugins and before the IdeGYM server plugin so it
sees the final runtime user and workspace.
"""

from importlib.resources.abc import Traversable
from typing import Optional

from idegym.api.plugin import BuildContext, PluginBase, image_plugin
from idegym.plugins.openhands.api.models import Profile, TerminalBackend
from idegym.plugins.openhands.api.names import PLUGIN_NAME
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.plugin_utils import plugin_asset
from pydantic import field_validator

# COPY destinations, relative to the Docker build context, mirroring the repo layout so the local
# build driver (get_context_files) and the Kaniko git-context path resolve the same paths.
_START_SCRIPT_DEST = "plugins/openhands/scripts/start-openhands-service.sh"
_SUPERVISOR_DEST = "plugins/openhands/scripts/supervisord-openhands.conf"
# The IdeGYM server base image already bakes the plugin source in at ``$IDEGYM_PATH/plugins`` (the
# server Dockerfile COPYs the workspace there and installs it). The dedicated venv installs
# idegym-plugins from that same in-image source, so nothing is fetched or copied a second time.
_IN_IMAGE_PLUGINS_SRC = "$IDEGYM_PATH/plugins"


@image_plugin(PLUGIN_NAME)
class OpenHands(PluginBase):
    """Install and start the agentless OpenHands Tools Service and expose it to IdeGYM.

    Attributes:
        service_port: Loopback port for the internal service (never published externally).
        profile: ``core`` (terminal + file/search/patch/gemini tools) or ``full`` (adds browser).
        enabled_tools / disabled_tools: Explicit allow/deny for the ``custom`` profile.
        default_terminal_backend / allowed_terminal_backends: Terminal backend policy.
        openhands_sdk_version / openhands_tools_version: Pinned upstream versions.
        package_spec: Optional override for how the dedicated venv installs this plugin's runtime.
            When unset, the venv installs idegym-plugins from the source the base image already bakes
            in at ``$IDEGYM_PATH/plugins`` (like the idea/pycharm plugins, whose code the base image
            provides). Set it to a published requirement (e.g. ``"idegym-plugins==X.Y"``) or a wheel
            path to install from PyPI instead once idegym-plugins is published there.
    """

    service_port: int = 8900
    profile: Profile = Profile.CORE
    enabled_tools: tuple[str, ...] = ()
    disabled_tools: tuple[str, ...] = ()

    default_terminal_backend: TerminalBackend = TerminalBackend.TMUX
    allowed_terminal_backends: tuple[TerminalBackend, ...] = (TerminalBackend.TMUX, TerminalBackend.SUBPROCESS)
    auto_create_default_terminal: bool = False
    auto_recreate_lost_terminal: bool = False
    subprocess_shell: str = "/bin/bash"

    state_dir: str = "/var/lib/idegym-openhands/state"
    output_dir: str = "/var/lib/idegym-openhands/artifacts"
    log_dir: str = "/var/log/idegym-openhands"
    tmux_socket_dir: str = "/tmp/idegym-openhands-tmux"

    max_terminals: int = 32
    max_output_bytes: int = 64_000
    no_change_timeout_seconds: float = 30.0
    initial_environment_allowlist: tuple[str, ...] = ()
    browser_enabled: bool = False

    openhands_sdk_version: str = compat.PINNED_OPENHANDS_SDK
    openhands_tools_version: str = compat.PINNED_OPENHANDS_TOOLS
    # Dedicated in-container virtualenv for the service. It is isolated from the IdeGYM server's
    # environment because OpenHands transitively pins an older opentelemetry-api than idegym uses.
    venv_dir: str = "/opt/idegym-openhands"
    # Install the OpenHands runtime dependencies into the dedicated venv. Disable to build a
    # structural-only image (fast tests that do not need the heavy OpenHands stack).
    install_openhands: bool = True
    # How to install this plugin's code (idegym-plugins) into the dedicated venv. ``None`` copies the
    # local ``plugins/`` source from the build context and installs it (idegym-plugins is not on
    # PyPI); set a pip requirement (e.g. a pinned published version or a wheel path) to override.
    package_spec: Optional[str] = None
    # Run the build-time smoke check (imports adapters, exercises a subprocess terminal, checks tmux).
    build_smoke_test: bool = True

    user: Optional[str] = None

    @field_validator("allowed_terminal_backends")
    @classmethod
    def _non_empty(cls, v: tuple[TerminalBackend, ...]) -> tuple[TerminalBackend, ...]:
        if not v:
            raise ValueError("allowed_terminal_backends must not be empty")
        return v

    # -- integration points -------------------------------------------------

    def apply(self, ctx: BuildContext) -> BuildContext:
        existing = list(ctx.get_extra("idegym.enabled_server_plugins", []))
        if PLUGIN_NAME not in existing:
            existing.append(PLUGIN_NAME)
        return ctx.with_extra("idegym.enabled_server_plugins", existing)

    def get_mcp_upstream(self, ctx: BuildContext) -> Optional[str]:
        # Loopback only; the internal service port is never published outside the container.
        return f"http://127.0.0.1:{self.service_port}/mcp"

    def get_context_files(self, ctx: BuildContext) -> dict[str, Traversable]:
        # Packaged assets shipped in the wheel (force-include) with an editable-checkout fallback.
        return {
            _START_SCRIPT_DEST: plugin_asset(__package__, "scripts", "start-openhands-service.sh"),
            _SUPERVISOR_DEST: plugin_asset(__package__, "scripts", "supervisord-openhands.conf"),
        }

    # -- Dockerfile fragment ------------------------------------------------

    def render(self, ctx: BuildContext) -> str:
        user = self.user or ctx.current_user
        tmux_enabled = TerminalBackend.TMUX in self.allowed_terminal_backends
        venv_python = f"{self.venv_dir}/bin/python"

        system_pkgs = ["ca-certificates", "curl", "git"]
        if tmux_enabled:
            system_pkgs.append("tmux")

        dirs = f"{self.state_dir} {self.output_dir} {self.log_dir} {self.tmux_socket_dir}"
        env_lines = "\n".join(f"ENV {name}={value}" for name, value in self._env_pairs(ctx).items())

        parts = [
            "USER root",
            "",
            "# --- OpenHands Tools Service ---",
            "RUN set -eux; \\",
            "    apt-get update; \\",
            f"    apt-get install -y --no-install-recommends {' '.join(system_pkgs)}; \\",
            "    rm -rf /var/lib/apt/lists/*",
        ]

        if self.install_openhands:
            # A dedicated venv keeps OpenHands' dependency tree out of the IdeGYM server env. It is
            # created with uv (already on the base image at /bin/uv) so it uses the same Python 3.12+
            # the server runs, not the distro's system python3.
            pip_targets = [
                f'"openhands-sdk=={self.openhands_sdk_version}"',
                f'"openhands-tools=={self.openhands_tools_version}"',
                '"fastapi"',
                '"fastmcp>=3"',
                '"mcp"',
                '"uvicorn"',
                '"httpx"',
            ]
            if self.package_spec:
                # Explicit override: a published requirement (e.g. "idegym-plugins==X.Y") or a wheel.
                pip_targets.append(f'"{self.package_spec}"')
            else:
                # Reuse the plugin source the base image already baked in, rather than copying it
                # again. Its idegym-api/idegym-common-utils deps resolve from PyPI.
                pip_targets.append(f'"{_IN_IMAGE_PLUGINS_SRC}"')
            parts += [
                "",
                "RUN set -eux; \\",
                f"    uv venv --python 3.12 {self.venv_dir}; \\",
                f"    uv pip install --python {self.venv_dir}/bin/python --no-cache-dir {' '.join(pip_targets)}",
            ]

        chown_targets = f"{dirs} {self.venv_dir}" if self.install_openhands else dirs
        parts += [
            "",
            "RUN set -eux; \\",
            f"    mkdir -p {dirs}; \\",
            f"    chown -R {user}:{user} {chown_targets}",
            "",
            # Install to the bare command name (not .sh) so the supervisor command is valid even in
            # images that do not run IdeGYMServer's /usr/local/bin/*.{py,sh} -> bare rename pass.
            f"COPY {_START_SCRIPT_DEST} /usr/local/bin/start-openhands-service",
            f"COPY {_SUPERVISOR_DEST} /etc/supervisor/conf.d/openhands.conf",
            "RUN chmod +x /usr/local/bin/start-openhands-service",
            "",
            env_lines,
        ]

        if tmux_enabled and self.install_openhands:
            parts += ["", "RUN set -eux; tmux -V"]
        if self.build_smoke_test and self.install_openhands:
            parts += ["", f"RUN set -eux; {venv_python} -m idegym.plugins.openhands.service.smoke"]

        parts += ["", f"USER {user}"]
        return "\n".join(parts)

    def _env_pairs(self, ctx: BuildContext) -> dict[str, str]:
        workspace = ctx.project_root
        allowed = ",".join(b.value for b in self.allowed_terminal_backends)
        pairs = {
            "IDEGYM_OPENHANDS_PYTHON": f"{self.venv_dir}/bin/python",
            "IDEGYM_OPENHANDS_SERVICE_PORT": str(self.service_port),
            "IDEGYM_OPENHANDS_WORKSPACE_ROOT": workspace,
            "IDEGYM_OPENHANDS_STATE_DIR": self.state_dir,
            "IDEGYM_OPENHANDS_OUTPUT_DIR": self.output_dir,
            "IDEGYM_OPENHANDS_LOG_DIR": self.log_dir,
            "IDEGYM_OPENHANDS_TMUX_SOCKET_DIR": self.tmux_socket_dir,
            "IDEGYM_OPENHANDS_PROFILE": self.profile.value,
            "IDEGYM_OPENHANDS_DEFAULT_TERMINAL_BACKEND": self.default_terminal_backend.value,
            "IDEGYM_OPENHANDS_ALLOWED_TERMINAL_BACKENDS": allowed,
            "IDEGYM_OPENHANDS_SUBPROCESS_SHELL": self.subprocess_shell,
            "IDEGYM_OPENHANDS_MAX_TERMINALS": str(self.max_terminals),
            "IDEGYM_OPENHANDS_MAX_OUTPUT_BYTES": str(self.max_output_bytes),
            "IDEGYM_OPENHANDS_NO_CHANGE_TIMEOUT_SECONDS": str(self.no_change_timeout_seconds),
            "IDEGYM_OPENHANDS_AUTO_CREATE_DEFAULT_TERMINAL": "true" if self.auto_create_default_terminal else "false",
            "IDEGYM_OPENHANDS_AUTO_RECREATE_LOST_TERMINAL": "true" if self.auto_recreate_lost_terminal else "false",
            "IDEGYM_OPENHANDS_BROWSER_ENABLED": "true" if self.browser_enabled else "false",
        }
        if self.enabled_tools:
            pairs["IDEGYM_OPENHANDS_ENABLED_TOOLS"] = ",".join(self.enabled_tools)
        if self.disabled_tools:
            pairs["IDEGYM_OPENHANDS_DISABLED_TOOLS"] = ",".join(self.disabled_tools)
        if self.initial_environment_allowlist:
            pairs["IDEGYM_OPENHANDS_INITIAL_ENVIRONMENT_ALLOWLIST"] = ",".join(self.initial_environment_allowlist)
        return pairs
