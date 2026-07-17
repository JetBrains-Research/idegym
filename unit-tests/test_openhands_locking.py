"""Tool lock-policy tests: how ToolRuntime maps a tool call onto scheduler lock requests."""

import asyncio

import pytest
from idegym.plugins.openhands.api.models import TerminalBackend
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.service import ToolRuntime

pytestmark = pytest.mark.unit

SUB = TerminalBackend.SUBPROCESS


def _runtime(tmp_path):
    rt = ToolRuntime(
        RuntimeConfig(
            workspace_root=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            output_dir=str(tmp_path / "art"),
            log_dir=str(tmp_path / "log"),
            default_terminal_backend=SUB,
            allowed_terminal_backends=[SUB],
            no_change_timeout_seconds=1.5,
        )
    )
    rt.prepare()
    return rt


def test_read_shares_the_file_lock_write_holds_it_exclusive(tmp_path):
    rt = _runtime(tmp_path)
    args = {"file_path": str(tmp_path / "f.txt")}
    read_reqs = rt._lock_requests(rt.catalog.get("read_file"), args)
    write_reqs = rt._lock_requests(rt.catalog.get("write_file"), args)

    file_key = next(k for k, _ in read_reqs if k.startswith("file:"))
    assert (file_key, False) in read_reqs  # a read takes the file lock shared
    assert (file_key, True) in write_reqs  # a write takes it exclusive, so read/write still exclude


async def test_concurrent_reads_run_in_parallel_but_a_write_excludes_them(tmp_path):
    rt = _runtime(tmp_path)
    args = {"file_path": str(tmp_path / "f.txt")}
    read = rt.catalog.get("read_file")
    write = rt.catalog.get("write_file")

    async def measure(entries):
        active = 0
        peak = 0

        async def op(entry):
            nonlocal active, peak
            async with rt.scheduler.acquire(rt._lock_requests(entry, args)):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1

        await asyncio.gather(*(op(e) for e in entries))
        return peak

    assert await measure([read, read, read]) == 3  # concurrent reads of the same file run at once
    assert await measure([read, write]) == 1  # a write to the same file still serializes with a read
