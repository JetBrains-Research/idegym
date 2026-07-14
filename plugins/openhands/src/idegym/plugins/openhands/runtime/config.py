"""Runtime configuration, resolved from environment variables at service startup.

The image plugin renders the environment (workspace/state/output paths, backend policy, profile)
into the container; the service reads it here. Defaults keep the service runnable in a plain dev
checkout for tests.
"""

import os
from pathlib import Path
from typing import Optional

from idegym.plugins.openhands.api.models import Profile, TerminalBackend
from pydantic import BaseModel, Field, field_validator

_PREFIX = "IDEGYM_OPENHANDS_"


def _env(name: str, default: str) -> str:
    return os.environ.get(_PREFIX + name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(_PREFIX + name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(_PREFIX + name)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class RuntimeConfig(BaseModel):
    """Resolved configuration for the OpenHands Tools Service."""

    service_host: str = "127.0.0.1"
    service_port: int = 8900

    workspace_root: str = "/root/work"
    state_dir: str = "/var/lib/idegym-openhands/state"
    output_dir: str = "/var/lib/idegym-openhands/artifacts"
    log_dir: str = "/var/log/idegym-openhands"

    profile: Profile = Profile.CORE
    enabled_tools: list[str] = Field(default_factory=list)
    disabled_tools: list[str] = Field(default_factory=list)

    default_terminal_backend: TerminalBackend = TerminalBackend.TMUX
    allowed_terminal_backends: list[TerminalBackend] = Field(
        default_factory=lambda: [TerminalBackend.TMUX, TerminalBackend.SUBPROCESS]
    )
    auto_create_default_terminal: bool = False
    auto_recreate_lost_terminal: bool = False
    strict_backend_availability: bool = False

    max_terminals: int = 32
    max_output_bytes: int = 64_000
    no_change_timeout_seconds: float = 30.0
    hard_timeout_seconds: float = 900.0

    subprocess_shell: str = "/bin/bash"
    tmux_socket_dir: str = "/tmp/idegym-openhands-tmux"

    # Allowlist of environment-variable names a caller may set when creating a terminal.
    initial_environment_allowlist: list[str] = Field(default_factory=list)

    # Reject file-tool paths that resolve outside the workspace root.
    enforce_workspace_boundary: bool = True

    browser_enabled: bool = False

    # Artifact retention.
    max_artifacts: int = 256
    max_artifact_bytes: int = 512_000_000

    # Deduplication cache size per environment.
    dedup_cache_size: int = 512

    @field_validator("allowed_terminal_backends")
    @classmethod
    def _non_empty_backends(cls, v: list[TerminalBackend]) -> list[TerminalBackend]:
        if not v:
            raise ValueError("allowed_terminal_backends must not be empty")
        return v

    def model_post_init(self, _context: object) -> None:
        # The default backend must be in the allowlist; never silently substitute.
        if self.default_terminal_backend not in self.allowed_terminal_backends:
            raise ValueError(
                f"default_terminal_backend {self.default_terminal_backend!r} is not in "
                f"allowed_terminal_backends {self.allowed_terminal_backends!r}"
            )

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        """Build a config from ``IDEGYM_OPENHANDS_*`` environment variables."""
        allowed = [TerminalBackend(b) for b in _env_list("ALLOWED_TERMINAL_BACKENDS", ["tmux", "subprocess"])]
        default = TerminalBackend(_env("DEFAULT_TERMINAL_BACKEND", allowed[0].value))
        return cls(
            service_host=_env("SERVICE_HOST", "127.0.0.1"),
            service_port=int(_env("SERVICE_PORT", "8900")),
            workspace_root=_env("WORKSPACE_ROOT", os.environ.get("IDEGYM_PROJECT_ROOT", "/root/work")),
            state_dir=_env("STATE_DIR", "/var/lib/idegym-openhands/state"),
            output_dir=_env("OUTPUT_DIR", "/var/lib/idegym-openhands/artifacts"),
            log_dir=_env("LOG_DIR", "/var/log/idegym-openhands"),
            profile=Profile(_env("PROFILE", "core")),
            enabled_tools=_env_list("ENABLED_TOOLS", []),
            disabled_tools=_env_list("DISABLED_TOOLS", []),
            default_terminal_backend=default,
            allowed_terminal_backends=allowed,
            auto_create_default_terminal=_env_bool("AUTO_CREATE_DEFAULT_TERMINAL", False),
            auto_recreate_lost_terminal=_env_bool("AUTO_RECREATE_LOST_TERMINAL", False),
            strict_backend_availability=_env_bool("STRICT_BACKEND_AVAILABILITY", False),
            max_terminals=int(_env("MAX_TERMINALS", "32")),
            max_output_bytes=int(_env("MAX_OUTPUT_BYTES", "64000")),
            no_change_timeout_seconds=float(_env("NO_CHANGE_TIMEOUT_SECONDS", "30")),
            hard_timeout_seconds=float(_env("HARD_TIMEOUT_SECONDS", "900")),
            subprocess_shell=_env("SUBPROCESS_SHELL", "/bin/bash"),
            tmux_socket_dir=_env("TMUX_SOCKET_DIR", "/tmp/idegym-openhands-tmux"),
            initial_environment_allowlist=_env_list("INITIAL_ENVIRONMENT_ALLOWLIST", []),
            enforce_workspace_boundary=_env_bool("ENFORCE_WORKSPACE_BOUNDARY", True),
            browser_enabled=_env_bool("BROWSER_ENABLED", False),
        )

    def ensure_directories(self) -> None:
        """Create the state/output/log directories."""
        for path in (self.state_dir, self.output_dir, self.log_dir):
            Path(path).mkdir(parents=True, exist_ok=True)

    def resolve_cwd(self, cwd: Optional[str]) -> str:
        """Resolve an initial cwd for a terminal, defaulting to the workspace root."""
        return cwd or self.workspace_root
