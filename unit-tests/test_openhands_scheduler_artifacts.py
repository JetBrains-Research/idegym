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
        async with sched.acquire([("file:/a", True)]):
            order.append(f"start-{tag}")
            await asyncio.sleep(hold)
            order.append(f"end-{tag}")

    await asyncio.gather(worker("1", 0.05), worker("2", 0.0))
    # Same exclusive key must serialize: one worker fully completes before the other starts.
    assert order in (["start-1", "end-1", "start-2", "end-2"], ["start-2", "end-2", "start-1", "end-1"])


async def test_scheduler_parallel_different_keys():
    sched = ResourceScheduler()
    active = 0
    peak = 0

    async def worker(key: str):
        nonlocal active, peak
        async with sched.acquire([(key, True)]):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1

    await asyncio.gather(worker("file:/a"), worker("file:/b"))
    assert peak == 2  # distinct keys run concurrently


async def test_scheduler_sorted_acquisition_is_deadlock_free():
    sched = ResourceScheduler()

    async def ab():
        async with sched.acquire([("b", True), ("a", True)]):
            await asyncio.sleep(0.02)

    async def ba():
        async with sched.acquire([("a", True), ("b", True)]):
            await asyncio.sleep(0.02)

    # Both sort keys to [a, b] before acquiring, so there is no lock-ordering deadlock.
    await asyncio.wait_for(asyncio.gather(ab(), ba()), timeout=2.0)


async def test_scheduler_workspace_mutation_excludes_file_ops():
    # OH-10: a workspace mutation (workspace exclusive) must not overlap a contained file op
    # (workspace shared), even though the exact keys differ.
    sched = ResourceScheduler()
    order: list[str] = []

    async def mutate():
        async with sched.acquire([("workspace:w", True)]):
            order.append("mutate-start")
            await asyncio.sleep(0.05)
            order.append("mutate-end")

    async def write():
        async with sched.acquire([("workspace:w", False), ("file:/a", True)]):
            order.append("write-start")
            order.append("write-end")

    await asyncio.gather(mutate(), write())
    assert order in (
        ["mutate-start", "mutate-end", "write-start", "write-end"],
        ["write-start", "write-end", "mutate-start", "mutate-end"],
    )


async def test_scheduler_shared_reads_run_parallel_but_same_file_serializes():
    # OH-10: shared workspace reads run concurrently; a same-file exclusive lock serializes.
    sched = ResourceScheduler()

    async def measure(requests):
        active = 0
        peak = 0

        async def op():
            nonlocal active, peak
            async with sched.acquire(requests):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.05)
                active -= 1

        await asyncio.gather(op(), op(), op())
        return peak

    # three shared-workspace reads run at once
    assert await measure([("workspace:w", False)]) == 3
    # two ops on the same exclusive file lock serialize (peak concurrency 1)
    sched2 = ResourceScheduler()

    async def op2():
        async with sched2.acquire([("workspace:w", False), ("file:/a", True)]):
            op2.active += 1
            op2.peak = max(op2.peak, op2.active)
            await asyncio.sleep(0.03)
            op2.active -= 1

    op2.active = 0
    op2.peak = 0
    await asyncio.gather(op2(), op2())
    assert op2.peak == 1


async def test_scheduler_registry_is_bounded_and_correct():
    # OH-15: acquiring/releasing many unique keys must not grow the registry without bound.
    sched = ResourceScheduler()
    for i in range(10_000):
        async with sched.acquire([(f"file:/f{i}", True)]):
            pass
    assert len(sched._locks) == 0  # every entry removed once unreferenced


async def test_scheduler_contended_key_retained_until_all_release():
    # OH-15: a key stays registered while a holder or waiter references it, then is removed.
    sched = ResourceScheduler()
    release_holder = asyncio.Event()

    async def holder():
        async with sched.acquire([("k", True)]):
            await release_holder.wait()

    async def waiter():
        async with sched.acquire([("k", True)]):
            pass

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.02)
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.02)
    assert "k" in sched._locks and sched._locks["k"].refs == 2  # holder + waiter
    release_holder.set()
    await asyncio.gather(h, w)
    assert "k" not in sched._locks


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
