"""Unit tests for the resource scheduler and the artifact store."""

import asyncio

import pytest
from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.runtime.artifacts import ArtifactStore
from idegym.plugins.openhands.runtime.scheduler import ResourceScheduler

pytestmark = pytest.mark.unit


async def test_scheduler_serializes_same_key():
    sched = ResourceScheduler()
    order: list[str] = []

    async def worker(tag: str, hold: float):
        async with sched.acquire(["file:/a"]):
            order.append(f"start-{tag}")
            await asyncio.sleep(hold)
            order.append(f"end-{tag}")

    await asyncio.gather(worker("1", 0.05), worker("2", 0.0))
    # Same key must serialize: one worker fully completes before the other starts.
    assert order in (["start-1", "end-1", "start-2", "end-2"], ["start-2", "end-2", "start-1", "end-1"])


async def test_scheduler_parallel_different_keys():
    sched = ResourceScheduler()
    active = 0
    peak = 0

    async def worker(key: str):
        nonlocal active, peak
        async with sched.acquire([key]):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(worker("file:/a"), worker("file:/b"))
    assert peak == 2  # distinct keys run concurrently


async def test_scheduler_sorted_acquisition_is_deadlock_free():
    sched = ResourceScheduler()

    async def ab():
        async with sched.acquire(["b", "a"]):
            await asyncio.sleep(0.02)

    async def ba():
        async with sched.acquire(["a", "b"]):
            await asyncio.sleep(0.02)

    # Both sort keys to [a, b] before acquiring, so there is no lock-ordering deadlock.
    await asyncio.wait_for(asyncio.gather(ab(), ba()), timeout=2.0)


def test_artifact_save_read_and_url(tmp_path):
    store = ArtifactStore(str(tmp_path / "art"))
    desc = store.save_text("hello world", filename="out.txt")
    assert desc.url == f"/api/openhands/artifacts/{desc.artifact_id}"
    data, meta = store.read(desc.artifact_id)
    assert data == b"hello world" and meta.size_bytes == 11


def test_artifact_unknown_raises(tmp_path):
    store = ArtifactStore(str(tmp_path / "art"))
    with pytest.raises(ServiceError) as exc:
        store.read("nope")
    assert exc.value.code == ErrorCode.UNKNOWN_ARTIFACT


def test_artifact_retention_evicts_oldest(tmp_path):
    store = ArtifactStore(str(tmp_path / "art"), max_artifacts=2)
    a = store.save_text("a")
    store.save_text("b")
    store.save_text("c")  # evicts a
    with pytest.raises(ServiceError):
        store.read(a.artifact_id)


def test_artifact_clear(tmp_path):
    store = ArtifactStore(str(tmp_path / "art"))
    store.save_text("x")
    store.save_text("y")
    assert store.clear() == 2


def test_artifact_larger_than_total_budget_is_still_retrievable(tmp_path):
    # A single artifact exceeding max_total_bytes must not be evicted right after it is saved.
    store = ArtifactStore(str(tmp_path / "art"), max_total_bytes=4)
    desc = store.save_text("this is much larger than four bytes")
    data, _ = store.read(desc.artifact_id)
    assert data.decode() == "this is much larger than four bytes"
