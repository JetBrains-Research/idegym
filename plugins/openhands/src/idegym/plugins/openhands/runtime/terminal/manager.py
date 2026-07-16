"""Backend-neutral terminal session manager.

Creates, indexes, locks, resets, interrupts, and closes stateful terminal handles. Each handle owns
exactly one retained backend session (an OpenHands-managed tmux pane or subprocess process) for the
duration of a generation; the backend is fixed at creation and never silently switched. Operations
on a single handle are serialised; distinct handles run concurrently.
"""

import asyncio
import contextlib
import shutil
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import (
    BackendConfigView,
    CallStatus,
    ContentBlock,
    TerminalBackend,
    TerminalBackendStatus,
    TerminalCreateRequest,
    TerminalDescriptor,
    TerminalResult,
    TerminalState,
)
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.terminal.backend import BackendExec, TerminalBackendSession
from idegym.plugins.openhands.runtime.terminal.backends.openhands import OpenHandsTerminalSession
from idegym.plugins.openhands.runtime.terminal.backends.subprocess import SubprocessBackendSession
from idegym.plugins.openhands.runtime.terminal.handle import TerminalHandle

_DEFAULT_ID = "default"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Reservation:
    """A single-flight slot for an in-progress ``create``: joiners await the same startup."""

    __slots__ = ("event", "handle", "error")

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self.handle: Optional["TerminalHandle"] = None
        self.error: Optional[BaseException] = None

    def resolve(self, handle: "TerminalHandle") -> None:
        self.handle = handle
        self.event.set()

    def fail(self, error: BaseException) -> None:
        self.error = error
        self.event.set()


class TerminalSessionManager:
    def __init__(self, config: RuntimeConfig, environment_id_getter: Callable[[], str]) -> None:
        self._config = config
        self._environment_id = environment_id_getter
        self._handles: dict[str, TerminalHandle] = {}
        self._creating: dict[str, _Reservation] = {}
        self._registry_lock = asyncio.Lock()
        self._backend_status: dict[TerminalBackend, TerminalBackendStatus] = {}

    # -- availability -----------------------------------

    def probe_backends(self) -> dict[TerminalBackend, TerminalBackendStatus]:
        openhands_ok = compat.openhands_available()
        versions = compat.openhands_versions()
        result: dict[TerminalBackend, TerminalBackendStatus] = {}
        for backend in (TerminalBackend.TMUX, TerminalBackend.SUBPROCESS):
            enabled = backend in self._config.allowed_terminal_backends
            if backend == TerminalBackend.TMUX:
                tmux_bin = shutil.which("tmux") is not None
                available = openhands_ok and tmux_bin
                detail = None
                if not openhands_ok:
                    detail = "openhands-tools not installed; tmux terminals are OpenHands-managed"
                elif not tmux_bin:
                    detail = "tmux binary not found on PATH"
                result[backend] = TerminalBackendStatus(
                    backend=backend,
                    available=available,
                    enabled=enabled,
                    version=versions.get("openhands-tools"),
                    detail=detail,
                )
            else:
                # subprocess is always available: OpenHands' subprocess terminal when installed,
                # otherwise the native retained-shell fallback.
                result[backend] = TerminalBackendStatus(
                    backend=backend,
                    available=True,
                    enabled=enabled,
                    version=versions.get("openhands-tools") if openhands_ok else "native-fallback",
                    detail=None if openhands_ok else "using native retained-shell fallback (OpenHands absent)",
                )
        self._backend_status = result
        return result

    def backend_config_view(self) -> BackendConfigView:
        if not self._backend_status:
            self.probe_backends()
        return BackendConfigView(
            default=self._config.default_terminal_backend,
            allowed=list(self._config.allowed_terminal_backends),
            statuses=list(self._backend_status.values()),
        )

    def default_backend_ready(self) -> bool:
        if not self._backend_status:
            self.probe_backends()
        status = self._backend_status.get(self._config.default_terminal_backend)
        return bool(status and status.available)

    # -- creation -----------------------------------------------------------

    def _resolve_backend(self, requested: Optional[TerminalBackend]) -> TerminalBackend:
        backend = requested or self._config.default_terminal_backend
        if backend not in self._config.allowed_terminal_backends:
            raise ServiceError(
                ErrorCode.TERMINAL_BACKEND_DISABLED,
                f"Terminal backend {backend.value!r} is not allowed by this deployment",
                {"allowed": [b.value for b in self._config.allowed_terminal_backends]},
            )
        if not self._backend_status:
            self.probe_backends()
        status = self._backend_status.get(backend)
        if status is None or not status.available:
            raise ServiceError(
                ErrorCode.TERMINAL_BACKEND_UNAVAILABLE,
                f"Terminal backend {backend.value!r} is unavailable",
                {"reason": status.detail if status else "unknown"},
            )
        return backend

    def _make_session(
        self,
        backend: TerminalBackend,
        cwd: str,
        env: dict[str, str],
        *,
        no_change_timeout: float,
        cols: Optional[int],
        rows: Optional[int],
    ) -> TerminalBackendSession:
        no_change = int(no_change_timeout)
        if backend == TerminalBackend.TMUX or compat.openhands_available():
            # OpenHands' retained session (pinned tmux pane, or its subprocess terminal when present).
            return OpenHandsTerminalSession(
                backend=backend, work_dir=cwd, username=None, env=env, no_change_timeout_seconds=no_change
            )
        # Native retained-shell fallback (subprocess only, when OpenHands is not installed).
        return SubprocessBackendSession(
            shell=self._config.subprocess_shell, cwd=cwd, env=env, cols=cols or 120, rows=rows or 40
        )

    def _filter_env(self, env: dict[str, str]) -> dict[str, str]:
        """Apply the caller-env allowlist."""
        allow = set(self._config.initial_environment_allowlist)
        filtered: dict[str, str] = {}
        for key, value in env.items():
            if not key or not key.replace("_", "").isalnum():
                raise ServiceError(ErrorCode.INVALID_ARGUMENTS, f"Invalid environment variable name: {key!r}")
            if allow and key not in allow:
                continue
            filtered[key] = value
        return filtered

    def _validate_dimensions(self, cols: Optional[int], rows: Optional[int]) -> None:
        # Validate before allocating any OS resources.
        for name, value in (("cols", cols), ("rows", rows)):
            if value is not None and (not isinstance(value, int) or value <= 0 or value > 10_000):
                raise ServiceError(ErrorCode.INVALID_ARGUMENTS, f"Invalid terminal {name}: {value!r}")

    async def create(self, request: TerminalCreateRequest, *, terminal_id: Optional[str] = None) -> TerminalDescriptor:
        backend = self._resolve_backend(request.backend)
        cwd = self._config.resolve_cwd(request.cwd)
        env = self._filter_env(request.env)
        no_change_timeout = request.no_change_timeout or self._config.no_change_timeout_seconds
        self._validate_dimensions(request.cols, request.rows)

        # Reserve the id, or join an in-flight creation (single-flight for fixed ids like default).
        async with self._registry_lock:
            tid = terminal_id or TerminalHandle.new_id()
            existing = self._handles.get(tid)
            if existing is not None:
                return existing.descriptor()
            joined = self._creating.get(tid)
            if joined is None:
                # Count in-flight reservations against the quota so concurrent creates can't overshoot.
                if len(self._handles) + len(self._creating) >= self._config.max_terminals:
                    raise ServiceError(
                        ErrorCode.QUOTA_EXCEEDED, f"Terminal quota exceeded ({self._config.max_terminals})"
                    )
                reservation = _Reservation()
                self._creating[tid] = reservation
                creator = True
            else:
                reservation = joined
                creator = False

        if not creator:
            await reservation.event.wait()
            if reservation.error is not None:
                raise reservation.error
            assert reservation.handle is not None
            return reservation.handle.descriptor()

        # Build + start OUTSIDE the registry lock; publish only after startup succeeds.
        session: Optional[TerminalBackendSession] = None
        try:
            session = self._make_session(
                backend, cwd, env, no_change_timeout=no_change_timeout, cols=request.cols, rows=request.rows
            )
            handle = TerminalHandle(
                terminal_id=tid,
                backend=backend,
                workspace_root=self._config.workspace_root,
                initial_cwd=cwd,
                initial_env=env,
                environment_id=self._environment_id(),
                session=session,
                name=request.name,
                is_default=(tid == _DEFAULT_ID),
                no_change_timeout=no_change_timeout,
                cols=request.cols,
                rows=request.rows,
            )
            await session.start()
        except BaseException as exc:
            # Startup failed/cancelled: close any partial resource and free the reservation slot so
            # no stale handle or quota slot lingers; joiners observe the same failure.
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.close()
            async with self._registry_lock:
                self._creating.pop(tid, None)
            reservation.fail(exc)
            raise
        handle.last_working_dir = cwd
        async with self._registry_lock:
            self._handles[tid] = handle
            self._creating.pop(tid, None)
        reservation.resolve(handle)
        return handle.descriptor()

    async def ensure_default(self) -> TerminalHandle:
        existing = self._handles.get(_DEFAULT_ID)
        if existing is not None:
            return existing
        await self.create(TerminalCreateRequest(), terminal_id=_DEFAULT_ID)
        handle = self._handles.get(_DEFAULT_ID)
        if handle is None:
            raise ServiceError(ErrorCode.TERMINAL_LOST, "default terminal was removed during creation")
        return handle

    # -- lookups ------------------------------------------------------------

    def _require(self, terminal_id: str) -> TerminalHandle:
        handle = self._handles.get(terminal_id)
        if handle is None:
            raise ServiceError(ErrorCode.UNKNOWN_TERMINAL, f"Unknown terminal: {terminal_id}")
        return handle

    def list(self) -> list[TerminalDescriptor]:
        return [h.descriptor() for h in self._handles.values()]

    def get(self, terminal_id: str) -> TerminalDescriptor:
        return self._require(terminal_id).descriptor()

    # -- operations ---------------------------------------------------------

    def _apply(self, handle: TerminalHandle, res: BackendExec, *, call_id: str) -> TerminalResult:
        call_id = call_id or uuid.uuid4().hex
        handle.touch()
        if res.lost:
            handle.state = TerminalState.LOST
            status = CallStatus.LOST
            is_error = True
        elif res.running:
            handle.state = TerminalState.RUNNING
            status = CallStatus.RUNNING
            is_error = False
        else:
            handle.state = TerminalState.READY
            handle.last_exit_code = res.exit_code
            if res.cwd:
                handle.last_working_dir = res.cwd
            if res.interrupted:
                status = CallStatus.INTERRUPTED
            else:
                status = CallStatus.COMPLETED
            is_error = res.exit_code is not None and res.exit_code != 0
        return TerminalResult(
            call_id=call_id,
            terminal_id=handle.terminal_id,
            backend=handle.backend,
            generation=handle.generation,
            state=handle.state,
            status=status,
            is_error=is_error,
            output=res.output,
            running=res.running,
            exit_code=handle.last_exit_code if not res.running else None,
            working_dir=handle.last_working_dir,
            content=[ContentBlock.of_text(res.output)] if res.output else [],
            metadata={
                "terminal_id": handle.terminal_id,
                "backend": handle.backend.value,
                "generation": handle.generation,
                "environment_id": handle.environment_id,
            },
            started_at=_now(),
            finished_at=_now(),
        )

    async def execute(
        self,
        terminal_id: str,
        command: str,
        *,
        timeout: Optional[float] = None,
        is_input: bool = False,
        reset: bool = False,
        call_id: str = "",
    ) -> TerminalResult:
        handle = self._require(terminal_id)
        if reset:
            await self.reset(terminal_id)
            handle = self._require(terminal_id)
        timeout = self._default_timeout(handle) if timeout is None else timeout
        async with handle.op_lock:
            self._reject_if_closing(handle, terminal_id)
            if is_input:
                self._require_foreground(handle, terminal_id)
                res = await handle.session.input(command, timeout)
            else:
                # Reject a new command while a foreground command is still active.
                if handle.session.has_foreground_command:
                    raise ServiceError(
                        ErrorCode.TERMINAL_BUSY,
                        "A foreground command is active; send input, poll, interrupt, or reset first",
                        {"terminal_id": terminal_id},
                    )
                res = await handle.session.execute(command, timeout)
            return self._apply(handle, res, call_id=call_id)

    async def input(
        self, terminal_id: str, text: str, *, timeout: Optional[float] = None, call_id: str = ""
    ) -> TerminalResult:
        handle = self._require(terminal_id)
        timeout = self._default_timeout(handle) if timeout is None else timeout
        async with handle.op_lock:
            self._reject_if_closing(handle, terminal_id)
            self._require_foreground(handle, terminal_id)
            res = await handle.session.input(text, timeout)
            return self._apply(handle, res, call_id=call_id)

    def _reject_if_closing(self, handle: TerminalHandle, terminal_id: str) -> None:
        """Reject an operation that acquired the op lock after close() marked the handle CLOSING."""
        if handle.state in (TerminalState.CLOSING, TerminalState.CLOSED):
            raise ServiceError(ErrorCode.UNKNOWN_TERMINAL, f"Unknown terminal: {terminal_id}")

    def _require_foreground(self, handle: TerminalHandle, terminal_id: str) -> None:
        """Reject input when no foreground command is active.

        Writing ordinary text to an idle shell would start it as a new (untracked) command with no
        completion sentinel — corrupting protocol state so the next execute is consumed by it. Input
        is only meaningful while a tracked foreground command is running; interrupt/poll/reset are
        the idle-terminal operations.
        """
        if not handle.session.has_foreground_command:
            raise ServiceError(
                ErrorCode.TERMINAL_NOT_RUNNING,
                "No foreground command is active; terminal input is only accepted while a command runs",
                {"terminal_id": terminal_id},
            )

    def _default_timeout(self, handle: TerminalHandle) -> float:
        return handle.no_change_timeout or self._config.no_change_timeout_seconds

    async def poll(self, terminal_id: str, *, timeout: Optional[float] = None, call_id: str = "") -> TerminalResult:
        handle = self._require(terminal_id)
        timeout = 1.0 if timeout is None else timeout
        async with handle.op_lock:
            self._reject_if_closing(handle, terminal_id)
            res = await handle.session.poll(timeout)
            return self._apply(handle, res, call_id=call_id)

    async def interrupt(self, terminal_id: str, *, call_id: str = "") -> TerminalResult:
        # Interrupt must be able to signal an in-flight command without waiting on the execution
        # lock. Send the signal first, unconditionally.
        handle = self._require(terminal_id)
        await handle.session.interrupt()
        lock = handle.op_lock
        if lock.locked():
            # An execute/input is in flight and holds the lock. It owns the backend's output buffer,
            # so we must NOT poll here (that would race it for the completion sentinel). The in-flight
            # call observes the interrupt in its own drain and returns the result; we return an ack.
            handle.touch()
            return TerminalResult(
                call_id=call_id or uuid.uuid4().hex,
                terminal_id=handle.terminal_id,
                backend=handle.backend,
                generation=handle.generation,
                state=handle.state,
                status=CallStatus.INTERRUPTED,
                running=handle.state == TerminalState.RUNNING,
                metadata={"terminal_id": handle.terminal_id, "in_flight": True},
                started_at=_now(),
                finished_at=_now(),
            )
        # No command in flight: drain post-interrupt output under the lock so the shell becomes usable.
        async with lock:
            res = await handle.session.poll(1.0)
            res.interrupted = True
            return self._apply(handle, res, call_id=call_id)

    async def capture(self, terminal_id: str) -> str:
        handle = self._require(terminal_id)
        return await handle.session.capture()

    async def reset(self, terminal_id: str) -> TerminalDescriptor:
        handle = self._require(terminal_id)
        async with handle.op_lock:
            self._reject_if_closing(handle, terminal_id)
            old_session = handle.session
            new_session = self._make_session(
                handle.backend,
                handle.initial_cwd,
                handle.initial_env,
                no_change_timeout=self._default_timeout(handle),
                cols=handle.cols,
                rows=handle.rows,
            )
            # Start the replacement BEFORE swapping it in; on failure the old session is untouched
            # so the handle stays usable rather than pointing at a dead, READY-marked session.
            try:
                await new_session.start()
            except BaseException:
                with contextlib.suppress(Exception):
                    await new_session.close()
                raise
            # Replacement is live: atomically swap, then tear the old session down.
            handle.session = new_session
            handle.generation += 1
            handle.state = TerminalState.READY
            handle.last_exit_code = None
            handle.last_working_dir = handle.initial_cwd
            handle.touch()
            with contextlib.suppress(Exception):
                await old_session.close()
        return handle.descriptor()

    async def close(self, terminal_id: str) -> None:
        # Mark CLOSING while still registered so a new op that races in observes it, then close under
        # the handle's op lock so no backend method runs concurrently with the close.
        async with self._registry_lock:
            handle = self._handles.get(terminal_id)
            if handle is None:
                return
            handle.state = TerminalState.CLOSING
        async with handle.op_lock:
            with contextlib.suppress(Exception):
                await handle.session.close()
            handle.state = TerminalState.CLOSED
            async with self._registry_lock:
                if self._handles.get(terminal_id) is handle:
                    del self._handles[terminal_id]

    async def _close_handle(self, handle: TerminalHandle) -> None:
        async with handle.op_lock:
            with contextlib.suppress(Exception):
                await handle.session.close()
            handle.state = TerminalState.CLOSED

    async def reset_all(self) -> int:
        # Snapshot + unregister atomically (blocks concurrent lookups from finding stale handles),
        # then close each under its own op lock, gathering so one failure cannot skip the rest.
        async with self._registry_lock:
            handles = list(self._handles.values())
            for handle in handles:
                handle.state = TerminalState.CLOSING
            self._handles.clear()
        await asyncio.gather(*(self._close_handle(h) for h in handles), return_exceptions=True)
        return len(handles)
