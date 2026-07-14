"""Common terminal backend abstraction shared by the tmux and subprocess implementations.

Both backends reuse the same command/input/poll/interrupt/capture lifecycle and the same
sentinel-based completion detection so the transport-independent
:class:`~idegym.plugins.openhands.runtime.terminal.manager.TerminalSessionManager` treats them
identically.
"""

import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from idegym.plugins.openhands.api.models import TerminalBackend

# A completion sentinel is appended to every command line so the backend can detect that the
# foreground command returned, capture its exit code, and read the shell's new working directory.
# The token is unique per command so a late-arriving sentinel is never mistaken for a newer one.
_SENTINEL_PREFIX = "__IDEGYM_OH_END__"


def new_token() -> str:
    return uuid.uuid4().hex


def build_command_line(command: str, token: str) -> str:
    """Wrap a user command so the shell emits a parseable completion sentinel after it returns.

    The sentinel is joined with ``;`` on the *same* input line as the command so the shell parses
    both before executing. This is what lets an interactive foreground program (e.g. ``python -q``)
    keep reading subsequent input while the trailing ``printf`` stays queued until the program
    exits.

    Limitation of this native fallback: a command that ends with ``&`` (backgrounding) or a trailing
    unquoted comment makes ``<command> ; printf ...`` a shell parse error, so no sentinel is emitted
    and the call reports ``running`` until it times out. The OpenHands-backed backend, used whenever
    ``openhands-tools`` is installed, is unaffected.
    """
    marker = f'printf \'\\n{_SENTINEL_PREFIX}%s|%s|{token}\\n\' "$?" "$PWD"'
    return f"{command} ; {marker}\n"


def sentinel_regex(token: str) -> re.Pattern[str]:
    """Regex matching the sentinel line for ``token`` (tolerant of pty ``\\r\\n``)."""
    return re.compile(
        r"\r?\n" + re.escape(_SENTINEL_PREFIX) + r"(-?\d+)\|(.*?)\|" + re.escape(token) + r"\r?\n",
        re.DOTALL,
    )


# Mapping of symbolic special keys accepted by ``input`` to raw control bytes.
SPECIAL_KEYS: dict[str, bytes] = {
    "C-c": b"\x03",
    "C-d": b"\x04",
    "C-z": b"\x1a",
    "C-l": b"\x0c",
    "C-a": b"\x01",
    "C-e": b"\x05",
    "C-u": b"\x15",
    "C-k": b"\x0b",
    "enter": b"\n",
    "return": b"\n",
    "tab": b"\t",
    "escape": b"\x1b",
    "esc": b"\x1b",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
}


@dataclass
class BackendExec:
    """Outcome of a single backend operation (execute/input/poll)."""

    output: str = ""
    running: bool = False
    exit_code: Optional[int] = None
    cwd: Optional[str] = None
    lost: bool = False
    interrupted: bool = False


@dataclass
class BackendHealth:
    backend: TerminalBackend
    alive: bool
    detail: dict = field(default_factory=dict)


class TerminalBackendSession(ABC):
    """One retained backend session (a tmux pane or a subprocess shell)."""

    backend: TerminalBackend
    capture_supported: bool = True

    @abstractmethod
    async def start(self) -> None:
        """Create the underlying shell/pane. Idempotent per session instance."""

    @abstractmethod
    async def execute(self, command: str, timeout: float) -> BackendExec:
        """Send a new command when no foreground command is active."""

    @abstractmethod
    async def input(self, text: str, timeout: float) -> BackendExec:
        """Send text or a special key to the current foreground command."""

    @abstractmethod
    async def poll(self, timeout: float) -> BackendExec:
        """Read additional output without sending a new command."""

    @abstractmethod
    async def interrupt(self) -> None:
        """Send SIGINT (Ctrl-C equivalent) to the foreground command only."""

    @abstractmethod
    async def capture(self) -> str:
        """Return current visible/recent terminal content."""

    @abstractmethod
    async def health(self) -> BackendHealth: ...

    @abstractmethod
    async def close(self) -> None:
        """Terminate the owned shell/pane and all descendant processes."""

    @property
    @abstractmethod
    def alive(self) -> bool: ...

    @property
    @abstractmethod
    def has_foreground_command(self) -> bool:
        """True while a command is running (soft-timed-out but not finished)."""
