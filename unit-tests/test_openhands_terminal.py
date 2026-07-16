"""Real-backend terminal contract tests.

These run against the actual retained-shell subprocess backend (no mocks). tmux-specific behaviour
is validated in the container image; here tmux is unavailable, so we assert the explicit
backend-unavailable error and no silent fallback.
"""

import asyncio
import os

import pytest
from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import CallStatus, TerminalBackend, TerminalCreateRequest, TerminalState
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.terminal.backends.subprocess import SubprocessBackendSession
from idegym.plugins.openhands.runtime.terminal.manager import TerminalSessionManager

pytestmark = pytest.mark.unit

SUB = TerminalBackend.SUBPROCESS


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
    # OH-07: text sent when no foreground command is active must be rejected (not started as an
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


async def test_native_output_history_is_bounded(tmp_path):
    # OH-13: many MB of output over repeated commands must not accumulate in the parse buffer, while
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
