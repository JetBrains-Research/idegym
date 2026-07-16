"""Terminal backend that reuses an OpenHands terminal session.

This backend does not manage tmux or a subprocess shell itself. It retains one OpenHands
``TerminalSession`` — pinned to a single tmux pane or a single subprocess process for the handle's
lifetime — and adapts OpenHands' ``TerminalAction``/``TerminalObservation`` semantics onto the
backend-neutral contract. All pane/process management, prompt parsing, timeout policy, special-key
handling, and interruption are OpenHands'.

The OpenHands session API is synchronous; calls run in a worker thread. Completion is detected from
the observation exit code: OpenHands returns a negative/absent exit code while a foreground command
is still running (no-change timeout), and ``>= 0`` once it completes. All upstream specifics live in
:mod:`idegym.plugins.openhands.runtime.compat`.
"""

import asyncio
from typing import Any, Optional

from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import TerminalBackend
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.terminal.backend import BackendExec, BackendHealth, TerminalBackendSession


class OpenHandsTerminalSession(TerminalBackendSession):
    """Adapts a retained OpenHands terminal session to the backend contract."""

    # OpenHands terminal sessions do not expose a pane/buffer capture, so the descriptor advertises
    # capture as unsupported for this backend (callers read output from execute/poll instead).
    capture_supported = False

    def __init__(
        self,
        *,
        backend: TerminalBackend,
        work_dir: str,
        username: Optional[str],
        env: dict[str, str],
        no_change_timeout_seconds: Optional[int] = None,
    ) -> None:
        self.backend = backend
        self._work_dir = work_dir
        self._username = username
        self._env = dict(env)
        self._no_change_timeout = no_change_timeout_seconds
        self._session: Any = None
        self._alive = False
        self._running = False

    async def start(self) -> None:
        self._session = await asyncio.to_thread(
            compat.build_terminal_session,
            work_dir=self._work_dir,
            terminal_type=self.backend.value,
            env=self._env,
            username=self._username,
            no_change_timeout_seconds=self._no_change_timeout,
        )
        self._alive = True

    def _run_sync(self, command: str, is_input: bool, timeout: Optional[float], reset: bool) -> BackendExec:
        action = compat.terminal_action(command, is_input=is_input, timeout=timeout, reset=reset)
        obs = self._session.execute(action)
        exit_code = getattr(obs, "exit_code", None)
        running = exit_code is None or exit_code < 0
        cwd = None
        if not running:
            metadata = getattr(obs, "metadata", None)
            cwd = getattr(metadata, "working_dir", None) or getattr(self._session, "cwd", None)
        return BackendExec(
            output=compat.observation_text(obs),
            running=running,
            exit_code=None if running else exit_code,
            cwd=cwd,
        )

    async def _run(self, command: str, is_input: bool, timeout: Optional[float], reset: bool = False) -> BackendExec:
        if not self._alive:
            return BackendExec(lost=True)
        try:
            res = await asyncio.to_thread(self._run_sync, command, is_input, timeout, reset)
        except Exception:
            self._alive = False
            return BackendExec(lost=True)
        self._running = res.running
        return res

    async def execute(self, command: str, timeout: float) -> BackendExec:
        return await self._run(command, is_input=False, timeout=timeout)

    async def input(self, text: str, timeout: float) -> BackendExec:
        # Defense in depth: reject input to an idle session (the manager rejects it first). ``poll``
        # uses ``_run`` directly, so this guard does not affect idle polling.
        if not self.has_foreground_command:
            raise ServiceError(
                ErrorCode.TERMINAL_NOT_RUNNING,
                "No foreground command is active; terminal input requires a running command",
            )
        return await self._run(text, is_input=True, timeout=timeout)

    async def poll(self, timeout: float) -> BackendExec:
        # An empty is_input action asks OpenHands for more output from the foreground command.
        return await self._run("", is_input=True, timeout=timeout)

    async def interrupt(self) -> None:
        if self._alive and self._session is not None:
            await asyncio.to_thread(self._session.interrupt)
            # OpenHands' interrupt blocks until the foreground command is signalled; the handle is
            # now ready for a new command. Clear our derived flag so an idle empty-poll (which reads
            # as exit_code < 0) can't leave the terminal wedged as busy.
            self._running = False

    async def capture(self) -> str:
        return ""

    async def health(self) -> BackendHealth:
        return BackendHealth(backend=self.backend, alive=self._alive, detail=compat.openhands_versions())

    async def close(self) -> None:
        session = self._session
        self._alive = False
        if session is not None:
            with_close = getattr(session, "close", None)
            if callable(with_close):
                await asyncio.to_thread(with_close)

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def has_foreground_command(self) -> bool:
        # Prefer OpenHands' authoritative view of the session; our derived flag is a fallback for
        # versions that do not expose is_running(). This keeps the "is a command in the foreground?"
        # gate correct after an interrupt, when an idle poll would otherwise look like a live command.
        is_running = getattr(self._session, "is_running", None)
        if callable(is_running):
            try:
                return bool(is_running())
            except Exception:
                return self._running
        if isinstance(is_running, bool):
            return is_running
        return self._running
