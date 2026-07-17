"""Cancellation-safety tests for adapter dispatch and its scheduler locks."""

import asyncio
import time

import pytest
from idegym.plugins.openhands.api.models import TerminalBackend
from idegym.plugins.openhands.runtime.adapters.base import AdapterRun
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.service import ToolRuntime

pytestmark = pytest.mark.unit

SUB = TerminalBackend.SUBPROCESS


def _config(tmp_path, **overrides):
    return RuntimeConfig(
        workspace_root=str(tmp_path),
        state_dir=str(tmp_path / "state"),
        output_dir=str(tmp_path / "art"),
        log_dir=str(tmp_path / "log"),
        default_terminal_backend=SUB,
        allowed_terminal_backends=[SUB],
        no_change_timeout_seconds=1.5,
        **overrides,
    )


class _ThreadAdapter:
    """A fake adapter that offloads to a real thread-pool worker, like OpenHands' ``acall``.

    The worker keeps running after the awaiting coroutine is cancelled, which is exactly the
    orphaned-work condition the drain guards against.
    """

    def __init__(self, name: str, log: list[str], hold: float) -> None:
        self.name = name
        self.family = "gemini"
        self._log = log
        self._hold = hold

    async def run(self, arguments: dict) -> AdapterRun:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._blocking, arguments)

    def _blocking(self, arguments: dict) -> AdapterRun:
        tag = arguments["tag"]
        self._log.append(f"start-{tag}")
        time.sleep(self._hold)
        self._log.append(f"end-{tag}")
        return AdapterRun(content=[], structured={}, is_error=False)


async def test_cancelled_adapter_call_drains_before_releasing_file_lock(tmp_path):
    # Two writes to the same file serialize on the per-file lock. If the first call is cancelled
    # mid-write, its worker keeps running; the lock must stay held until that worker drains so the
    # second write cannot interleave with it.
    rt = ToolRuntime(_config(tmp_path))
    rt.prepare()
    entry = rt.catalog.get("write_file")  # LockScope.PATH -> workspace shared + per-file exclusive
    log: list[str] = []
    adapter = _ThreadAdapter("write_file", log, hold=0.25)
    rt._adapters["write_file"] = adapter

    target = str(tmp_path / "shared.txt")
    a = asyncio.create_task(rt._call_adapter_tool(entry, {"file_path": target, "tag": "A"}))
    # Wait until A's worker has actually started running in the thread pool.
    while "start-A" not in log:
        await asyncio.sleep(0.01)

    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await a

    # B starts only after cancellation; it must not run until A's orphaned worker has drained.
    b = asyncio.create_task(rt._call_adapter_tool(entry, {"file_path": target, "tag": "B"}))
    _ = await b

    assert log == ["start-A", "end-A", "start-B", "end-B"], log


async def test_run_adapter_drained_waits_for_worker_on_cancel(tmp_path):
    # The drain helper propagates the cancellation but only after the offloaded worker completes.
    rt = ToolRuntime(_config(tmp_path))
    rt.prepare()
    log: list[str] = []
    adapter = _ThreadAdapter("write_file", log, hold=0.2)

    task = asyncio.create_task(rt._run_adapter_drained(adapter, {"tag": "X"}))
    while "start-X" not in log:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await task
    # By the time the cancellation surfaced, the worker had already run to completion.
    assert log == ["start-X", "end-X"], log
