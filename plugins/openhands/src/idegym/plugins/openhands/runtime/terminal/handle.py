"""In-memory terminal handle: descriptor state plus the retained backend session and its lock."""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Optional

from idegym.plugins.openhands.api.models import TerminalBackend, TerminalDescriptor, TerminalState
from idegym.plugins.openhands.runtime.terminal.backend import TerminalBackendSession


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class TerminalHandle:
    """One logical terminal: a stable id bound to one backend for the current generation."""

    terminal_id: str
    backend: TerminalBackend
    workspace_root: str
    initial_cwd: str
    initial_env: dict[str, str]
    environment_id: str
    session: TerminalBackendSession
    name: Optional[str] = None
    generation: int = 1
    state: TerminalState = TerminalState.READY
    is_default: bool = False
    created_at: datetime = field(default_factory=_now)
    last_activity_at: datetime = field(default_factory=_now)
    last_exit_code: Optional[int] = None
    last_working_dir: Optional[str] = None
    # Per-terminal creation options (applied on create and re-applied on reset).
    no_change_timeout: Optional[float] = None
    cols: Optional[int] = None
    rows: Optional[int] = None
    # Serializes all operations (execute/input/poll/interrupt/reset/close) on this handle. It lives
    # on the handle — not a side dict — so an operation captures it together with the handle and can
    # never hit a KeyError if the handle is concurrently removed from the registry.
    op_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def touch(self) -> None:
        self.last_activity_at = _now()

    def descriptor(self) -> TerminalDescriptor:
        """External view. Raw backend metadata (pids, sockets, panes) is never exposed."""
        return TerminalDescriptor(
            terminal_id=self.terminal_id,
            name=self.name,
            backend=self.backend,
            generation=self.generation,
            workspace_root=self.workspace_root,
            initial_cwd=self.initial_cwd,
            state=self.state,
            created_at=self.created_at,
            last_activity_at=self.last_activity_at,
            last_exit_code=self.last_exit_code,
            last_working_dir=self.last_working_dir,
            capture_supported=self.session.capture_supported,
            environment_id=self.environment_id,
            is_default=self.is_default,
        )
