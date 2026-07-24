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
import contextlib
from concurrent.futures import ThreadPoolExecutor
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
        # Bumped on every interrupt. A run that started before an interrupt must not overwrite the
        # running flag the interrupt cleared, so _run only trusts its observation when this is
        # unchanged across the call.
        self._interrupt_seq = 0
        # A dedicated single-thread executor serializes ALL synchronous session access (start /
        # execute / input / poll / close). The OpenHands session is not thread-safe, and
        # asyncio.to_thread() does not stop a worker on caller cancellation — a following request
        # would then enter the same session concurrently on a different pool thread. With one worker,
        # a cancelled caller only detaches from the result; the next call queues behind the still-
        # running job, so at most one synchronous session call is ever in flight. (interrupt() is
        # intentionally NOT routed here — it is a concurrent signal to an in-flight command.)
        self._worker: Optional[ThreadPoolExecutor] = None

    def _in_worker(self, fn: Any, *args: Any):
        # Always invoked from within a coroutine; use the running loop explicitly rather than the
        # deprecated get_event_loop(), which would create/return a loop off the running one.
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self._worker, fn, *args)

    async def start(self) -> None:
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oh-term")

        def _build() -> None:
            session = compat.build_terminal_session(
                work_dir=self._work_dir,
                terminal_type=self.backend.value,
                env=self._env,
                username=self._username,
                no_change_timeout_seconds=self._no_change_timeout,
            )
            # Record on self even if the awaiting start() is cancelled, so a later close() can still
            # tear the late-created session down (no leaked initialized session).
            self._session = session
            self._alive = True

        await self._in_worker(_build)

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
        if not self._alive or self._worker is None:
            return BackendExec(lost=True)
        seq = self._interrupt_seq
        try:
            res = await self._in_worker(self._run_sync, command, is_input, timeout, reset)
        except asyncio.CancelledError:
            # The caller detaches from the result, but the worker keeps running the session call to
            # completion; the next call queues behind it, so the session is never entered twice.
            raise
        except Exception:  # noqa: BLE001  # mark backend lost on any session error
            self._alive = False
            return BackendExec(lost=True)
        # An interrupt that fired while this call was in flight wins: never resurrect the running
        # flag it just cleared. Otherwise reflect what the observation reported.
        self._running = res.running if self._interrupt_seq == seq else False
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
            # Record the interrupt before signalling so a concurrent in-flight _run observes it and
            # does not overwrite the cleared running flag with its own (stale) observation.
            self._interrupt_seq += 1
            # Interrupt is a concurrent signal to an in-flight command, so it runs on a separate
            # thread (NOT the single worker, which is busy holding the execute it must interrupt).
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
        self._alive = False
        worker = self._worker
        if worker is None:
            return

        def _shutdown() -> None:
            session = self._session
            self._session = None
            if session is not None:
                with_close = getattr(session, "close", None)
                if callable(with_close):
                    with contextlib.suppress(Exception):
                        with_close()

        # Submit the close directly so it queues behind any in-flight/late session job on the single
        # worker (draining it), then shut the worker down. Do not let caller cancellation skip the
        # worker drain: the close job is already submitted and shutdown() is non-blocking.
        future = worker.submit(_shutdown)
        try:
            await asyncio.wrap_future(future)
        finally:
            worker.shutdown(wait=False)
            self._worker = None

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
            except Exception:  # noqa: BLE001  # fall back to cached running state
                return self._running
        if isinstance(is_running, bool):
            return is_running
        return self._running
