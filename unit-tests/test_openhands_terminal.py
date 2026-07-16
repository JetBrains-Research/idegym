"""Real-backend terminal contract tests.

These run against the actual retained-shell subprocess backend (no mocks). tmux-specific behaviour
is validated in the container image; here tmux is unavailable, so we assert the explicit
backend-unavailable error and no silent fallback.
"""

import asyncio
import os
import re
import time
from types import SimpleNamespace

import pytest
from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import CallStatus, TerminalBackend, TerminalCreateRequest, TerminalState
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.terminal.backend import BackendExec, BackendHealth, TerminalBackendSession
from idegym.plugins.openhands.runtime.terminal.backends.subprocess import SubprocessBackendSession
from idegym.plugins.openhands.runtime.terminal.manager import TerminalSessionManager

pytestmark = pytest.mark.unit

SUB = TerminalBackend.SUBPROCESS


class _FakeSession(TerminalBackendSession):
    """A controllable in-memory backend session for lifecycle/race tests (no real shell)."""

    backend = SUB
    capture_supported = True

    def __init__(
        self,
        *,
        fail_start: bool = False,
        gate: asyncio.Event = None,
        running: bool = False,
        slow: float = 0.0,
        close_raises: bool = False,
    ) -> None:
        self.started = False
        self.closed = False
        self.close_attempted = False
        self._fail_start = fail_start
        self._gate = gate
        self._running = running
        self._slow = slow
        self._close_raises = close_raises
        self.calls: list[str] = []
        # Shared in-flight counter across all backend methods, to detect the manager letting a close
        # overlap an execute/input/poll on the same session.
        self.inflight = 0
        self.max_inflight = 0

    def _enter(self) -> None:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)

    def _exit(self) -> None:
        self.inflight -= 1

    async def start(self) -> None:
        if self._gate is not None:
            await self._gate.wait()
        if self._fail_start:
            raise RuntimeError("start failed")
        self.started = True

    async def execute(self, command: str, timeout: float) -> BackendExec:
        self._enter()
        try:
            self.calls.append("execute")
            if self._slow:
                await asyncio.sleep(self._slow)
            return BackendExec(output="ok", running=False, exit_code=0)
        finally:
            self._exit()

    async def input(self, text: str, timeout: float) -> BackendExec:
        return BackendExec(output="ok", running=False, exit_code=0)

    async def poll(self, timeout: float) -> BackendExec:
        return BackendExec(output="", running=self._running)

    async def interrupt(self) -> None:
        self._running = False

    async def capture(self) -> str:
        return ""

    async def health(self) -> BackendHealth:
        return BackendHealth(backend=self.backend, alive=self.started and not self.closed)

    async def close(self) -> None:
        self.close_attempted = True
        self._enter()
        try:
            if self._slow:
                await asyncio.sleep(self._slow)
            if self._close_raises:
                raise RuntimeError("close failed")
            self.closed = True
        finally:
            self._exit()

    @property
    def alive(self) -> bool:
        return self.started and not self.closed

    @property
    def has_foreground_command(self) -> bool:
        return self._running


def _config(tmp_path, allowed=(SUB,), default=SUB):
    return RuntimeConfig(
        workspace_root=str(tmp_path),
        state_dir=str(tmp_path / "state"),
        output_dir=str(tmp_path / "art"),
        log_dir=str(tmp_path / "log"),
        default_terminal_backend=default,
        allowed_terminal_backends=list(allowed),
        no_change_timeout_seconds=1.5,
    )


@pytest.fixture
async def manager(tmp_path):
    mgr = TerminalSessionManager(_config(tmp_path), lambda: "env-1")
    mgr.probe_backends()
    yield mgr
    await mgr.reset_all()


async def _new(manager, **kw):
    return await manager.create(TerminalCreateRequest(backend=SUB, **kw))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def test_descriptor_reports_backend(manager):
    d = await _new(manager, name="t")
    assert d.backend == SUB and d.state == TerminalState.READY and d.generation == 1


async def test_cwd_persists(manager, tmp_path):
    os.makedirs(tmp_path / "sub", exist_ok=True)
    d = await _new(manager)
    await manager.execute(d.terminal_id, "cd sub")
    res = await manager.execute(d.terminal_id, "pwd")
    assert res.status == CallStatus.COMPLETED
    assert str(tmp_path / "sub") in res.output
    assert res.working_dir and res.working_dir.endswith("/sub")


async def test_env_persists(manager):
    d = await _new(manager)
    await manager.execute(d.terminal_id, "export MYVAR=hello123")
    res = await manager.execute(d.terminal_id, "echo VAR=$MYVAR")
    assert "hello123" in res.output


async def test_repl_running_input_and_eof(manager):
    d = await _new(manager)
    started = await manager.execute(d.terminal_id, "python3 -qi", timeout=1.5)
    assert started.running and started.status == CallStatus.RUNNING
    await manager.input(d.terminal_id, "x = 41")
    res = await manager.input(d.terminal_id, "print(x + 1)")
    assert "42" in res.output
    done = await manager.input(d.terminal_id, "C-d", timeout=3)
    assert not done.running and done.status == CallStatus.COMPLETED


async def test_long_process_interrupt_then_usable(manager):
    d = await _new(manager)
    running = await manager.execute(d.terminal_id, "sleep 30", timeout=1.0)
    assert running.running
    await manager.interrupt(d.terminal_id)
    after = await manager.execute(d.terminal_id, "echo still-alive")
    assert after.status == CallStatus.COMPLETED and "still-alive" in after.output


async def test_concurrent_interrupt_leaves_terminal_usable(manager):
    # Interrupt fired while an execute is still in flight must not race it for the output buffer.
    d = await _new(manager)
    exec_task = asyncio.create_task(manager.execute(d.terminal_id, "sleep 30", timeout=30))
    await asyncio.sleep(0.4)  # let the execute start and hold the per-terminal lock
    ack = await manager.interrupt(d.terminal_id)
    assert ack.status == CallStatus.INTERRUPTED
    await exec_task  # the in-flight execute observes the interrupt and returns
    after = await manager.execute(d.terminal_id, "echo recovered")
    assert after.status == CallStatus.COMPLETED and "recovered" in after.output


async def test_idle_input_rejected_and_shell_stays_usable(manager):
    # Text sent when no foreground command is active must be rejected (not started as an
    # untracked command), and a subsequent execute must run normally.
    d = await _new(manager)
    with pytest.raises(ServiceError) as exc:
        await manager.input(d.terminal_id, "cat")
    assert exc.value.code == ErrorCode.TERMINAL_NOT_RUNNING
    # is_input via execute is rejected the same way
    with pytest.raises(ServiceError) as exc2:
        await manager.execute(d.terminal_id, "cat", is_input=True)
    assert exc2.value.code == ErrorCode.TERMINAL_NOT_RUNNING
    # the shell is uncorrupted: the next real command runs normally
    res = await manager.execute(d.terminal_id, "echo alive")
    assert res.status == CallStatus.COMPLETED and "alive" in res.output


async def _fake_manager(tmp_path):
    mgr = TerminalSessionManager(_config(tmp_path), lambda: "env-1")
    mgr.probe_backends()
    return mgr


async def test_create_failure_leaves_no_handle_process_or_quota(tmp_path, monkeypatch):
    # A failed start() must leave no handle, no reservation, no leaked session, no quota use.
    mgr = await _fake_manager(tmp_path)
    made: list = []

    def make(*a, **k):
        s = _FakeSession(fail_start=True)
        made.append(s)
        return s

    monkeypatch.setattr(mgr, "_make_session", make)
    with pytest.raises(RuntimeError):
        await mgr.create(TerminalCreateRequest(backend=SUB))
    assert mgr.list() == []  # nothing published
    assert mgr._creating == {}  # reservation released
    assert made and made[0].closed  # partial resource cleaned up


async def test_handle_not_visible_until_started(tmp_path, monkeypatch):
    # A creating handle is not exposed as usable before start() completes.
    mgr = await _fake_manager(tmp_path)
    gate = asyncio.Event()
    monkeypatch.setattr(mgr, "_make_session", lambda *a, **k: _FakeSession(gate=gate))
    task = asyncio.create_task(mgr.create(TerminalCreateRequest(backend=SUB)))
    await asyncio.sleep(0.05)
    assert mgr.list() == []  # still starting -> not published
    gate.set()
    desc = await task
    assert mgr.get(desc.terminal_id).state == TerminalState.READY


async def test_concurrent_ensure_default_creates_exactly_one(tmp_path, monkeypatch):
    # Two concurrent ensure_default() must create exactly one session (single-flight).
    mgr = await _fake_manager(tmp_path)
    gate = asyncio.Event()
    made: list = []

    def make(*a, **k):
        s = _FakeSession(gate=gate)
        made.append(s)
        return s

    monkeypatch.setattr(mgr, "_make_session", make)
    t1 = asyncio.create_task(mgr.ensure_default())
    t2 = asyncio.create_task(mgr.ensure_default())
    await asyncio.sleep(0.05)
    gate.set()
    h1, h2 = await asyncio.gather(t1, t2)
    assert h1.terminal_id == h2.terminal_id == "default"
    assert len(made) == 1  # exactly one session created
    assert len(mgr.list()) == 1


async def test_reset_failure_keeps_old_session_usable(tmp_path, monkeypatch):
    # If the replacement fails to start, the old session stays usable (no dead READY handle).
    mgr = await _fake_manager(tmp_path)
    n = {"count": 0}

    def make(*a, **k):
        n["count"] += 1
        return _FakeSession(fail_start=(n["count"] == 2))  # first ok, reset replacement fails

    monkeypatch.setattr(mgr, "_make_session", make)
    d = await mgr.create(TerminalCreateRequest(backend=SUB))
    with pytest.raises(RuntimeError):
        await mgr.reset(d.terminal_id)
    assert mgr.get(d.terminal_id).generation == 1  # unchanged; old session retained
    res = await mgr.execute(d.terminal_id, "echo x")  # old session still works
    assert res.status == CallStatus.COMPLETED


async def test_close_serializes_with_inflight_operation(tmp_path, monkeypatch):
    # Close must not run the backend's close concurrently with an in-flight execute, and must
    # remove the handle only after cleanup completes.
    mgr = await _fake_manager(tmp_path)
    sess = _FakeSession(slow=0.05)
    monkeypatch.setattr(mgr, "_make_session", lambda *a, **k: sess)
    d = await mgr.create(TerminalCreateRequest(backend=SUB))
    exec_task = asyncio.create_task(mgr.execute(d.terminal_id, "cmd"))
    await asyncio.sleep(0)  # let the execute acquire the op lock
    close_task = asyncio.create_task(mgr.close(d.terminal_id))
    await asyncio.gather(exec_task, close_task, return_exceptions=True)
    assert sess.max_inflight == 1  # execute and close never overlapped on the session
    assert sess.closed
    assert mgr.list() == []  # handle removed after cleanup, not before


async def test_reset_all_closes_all_even_if_one_close_raises(tmp_path, monkeypatch):
    # One failing close() must not skip cleanup of the others.
    mgr = await _fake_manager(tmp_path)
    made: list = []

    def make(*a, **k):
        s = _FakeSession(close_raises=(len(made) == 0))  # first session raises on close
        made.append(s)
        return s

    monkeypatch.setattr(mgr, "_make_session", make)
    await mgr.create(TerminalCreateRequest(backend=SUB))
    await mgr.create(TerminalCreateRequest(backend=SUB))
    count = await mgr.reset_all()
    assert count == 2
    assert all(s.close_attempted for s in made)  # every session was closed despite the first raising
    assert mgr.list() == []


async def test_openhands_backend_serializes_session_under_cancellation(monkeypatch):
    # Cancelling a blocked execute must not let the next call enter the (non-thread-safe)
    # OpenHands session concurrently — the single worker keeps max concurrency at one.
    from idegym.plugins.openhands.runtime import compat
    from idegym.plugins.openhands.runtime.terminal.backends.openhands import OpenHandsTerminalSession

    state = {"cur": 0, "max": 0}

    class _FakeOHSession:
        cwd = "/w"

        def initialize(self):
            pass

        def execute(self, action):
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            time.sleep(0.2)  # a long blocking session call
            state["cur"] -= 1
            return SimpleNamespace(exit_code=0, content=[], metadata=SimpleNamespace(working_dir="/w"))

        def interrupt(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(compat, "build_terminal_session", lambda **k: _FakeOHSession())
    monkeypatch.setattr(compat, "terminal_action", lambda *a, **k: object())
    monkeypatch.setattr(compat, "observation_text", lambda obs: "")

    sess = OpenHandsTerminalSession(backend=TerminalBackend.TMUX, work_dir="/w", username=None, env={})
    await sess.start()
    try:
        blocked = asyncio.create_task(sess.execute("sleep", 5))
        await asyncio.sleep(0.05)  # let it enter the worker
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        # immediately issue another call: it must queue behind the still-running worker job
        res = await sess.execute("echo", 5)
        assert res.running is False
        assert state["max"] == 1  # never two concurrent session calls
    finally:
        await sess.close()


async def test_native_close_terminates_background_descendants(manager):
    # A detached/backgrounded child (its own process group under job control) must be killed
    # on close, not leaked into a later environment.
    d = await _new(manager)
    res = await manager.execute(d.terminal_id, "nohup sleep 60 >/dev/null 2>&1 & echo PID=$!")
    m = re.search(r"PID=(\d+)", res.output)
    assert m, res.output
    pid = int(m.group(1))
    assert _alive(pid)
    await manager.close(d.terminal_id)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    assert not _alive(pid), f"background pid {pid} survived terminal close"


async def test_native_output_history_is_bounded(tmp_path):
    # Many MB of output over repeated commands must not accumulate in the parse buffer, while
    # sentinel parsing and the bounded capture ring keep working.
    sess = SubprocessBackendSession(shell="/bin/bash", cwd=str(tmp_path), env={})
    await sess.start()
    try:
        for _ in range(10):
            res = await sess.execute("yes idegym | head -c 100000", timeout=10)
            assert not res.running  # each command completes (sentinel parsed)
        # consumed prefixes are dropped: the unread buffer holds only a small unparsed suffix,
        # not the ~1MB of accumulated history.
        assert len(sess._unread) < 20_000
        # the capture ring is bounded regardless of total output
        assert len(await sess.capture()) <= 8192
        # sentinel parsing still works after all that output
        final = await sess.execute("echo DONE-MARKER", timeout=10)
        assert "DONE-MARKER" in final.output
    finally:
        await sess.close()


async def test_result_has_call_id(manager):
    d = await _new(manager)
    res = await manager.execute(d.terminal_id, "echo x")
    assert res.call_id  # lifecycle routes pass call_id="" -> the manager mints one


async def test_two_handles_are_isolated(manager):
    a = await _new(manager)
    b = await _new(manager)
    await manager.execute(a.terminal_id, "export SHARED=fromA")
    res = await manager.execute(b.terminal_id, "echo GOT=[${SHARED:-unset}]")
    assert "GOT=[unset]" in res.output


async def test_concurrent_execution_different_handles(manager):
    a = await _new(manager)
    b = await _new(manager)
    results = await asyncio.gather(
        manager.execute(a.terminal_id, "echo A"),
        manager.execute(b.terminal_id, "echo B"),
    )
    assert "A" in results[0].output and "B" in results[1].output


async def test_new_command_while_foreground_active_is_rejected(manager):
    d = await _new(manager)
    running = await manager.execute(d.terminal_id, "python3 -qi", timeout=1.5)
    assert running.running
    with pytest.raises(ServiceError) as exc:
        await manager.execute(d.terminal_id, "echo nope")
    assert exc.value.code == ErrorCode.TERMINAL_BUSY


async def test_top_level_exit_marks_lost_without_recreation(manager):
    d = await _new(manager)
    res = await manager.execute(d.terminal_id, "exit", timeout=3)
    assert res.status == CallStatus.LOST and res.state == TerminalState.LOST
    again = await manager.execute(d.terminal_id, "echo after")
    assert again.status == CallStatus.LOST  # no silent recreation


async def test_reset_clears_state_and_bumps_generation(manager):
    d = await _new(manager)
    await manager.execute(d.terminal_id, "export GONE=1")
    desc = await manager.reset(d.terminal_id)
    assert desc.generation == 2 and desc.backend == SUB
    res = await manager.execute(d.terminal_id, "echo V=[${GONE:-empty}]")
    assert "V=[empty]" in res.output


async def test_tmux_request_when_unavailable_fails_without_fallback(tmp_path):
    # tmux allowed but OpenHands/tmux absent in this environment -> explicit error, no fallback.
    cfg = _config(tmp_path, allowed=(SUB, TerminalBackend.TMUX), default=SUB)
    mgr = TerminalSessionManager(cfg, lambda: "e")
    mgr.probe_backends()
    with pytest.raises(ServiceError) as exc:
        await mgr.create(TerminalCreateRequest(backend=TerminalBackend.TMUX))
    assert exc.value.code == ErrorCode.TERMINAL_BACKEND_UNAVAILABLE
    await mgr.reset_all()


async def test_disabled_backend_rejected(tmp_path):
    cfg = _config(tmp_path, allowed=(SUB,), default=SUB)
    mgr = TerminalSessionManager(cfg, lambda: "e")
    mgr.probe_backends()
    with pytest.raises(ServiceError) as exc:
        await mgr.create(TerminalCreateRequest(backend=TerminalBackend.TMUX))
    assert exc.value.code == ErrorCode.TERMINAL_BACKEND_DISABLED
    await mgr.reset_all()
