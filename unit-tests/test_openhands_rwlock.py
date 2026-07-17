"""Unit tests for the environment-lease reader/writer lock."""

import asyncio

import pytest
from idegym.plugins.openhands.runtime.rwlock import RWLock

pytestmark = pytest.mark.unit


async def test_shared_readers_run_concurrently():
    lock = RWLock()
    active = 0
    peak = 0

    async def reader():
        nonlocal active, peak
        async with lock.shared():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1

    await asyncio.gather(reader(), reader(), reader())
    assert peak == 3


async def test_exclusive_excludes_readers():
    lock = RWLock()
    order: list[str] = []

    async def writer():
        async with lock.exclusive():
            order.append("w-start")
            await asyncio.sleep(0.03)
            order.append("w-end")

    async def reader():
        async with lock.shared():
            order.append("r")

    await asyncio.gather(writer(), reader())
    # The reader may go first or after, but never overlaps the exclusive holder.
    assert order in (["w-start", "w-end", "r"], ["r", "w-start", "w-end"])


async def test_cancelled_exclusive_waiter_wakes_blocked_readers():
    # Writer preference makes a new reader block while a writer is queued. If that queued writer is
    # cancelled, it must wake the reader that blocked only on its presence; otherwise the reader
    # hangs until some unrelated release, which here never comes (the holder never lets go).
    lock = RWLock()
    release_holder = asyncio.Event()

    async def holder():
        async with lock.shared():
            await release_holder.wait()

    held = asyncio.create_task(holder())
    await asyncio.sleep(0.02)  # holder now owns the lock shared

    async def writer():
        async with lock.exclusive():
            pass

    w = asyncio.create_task(writer())
    await asyncio.sleep(0.02)  # writer registers as a queued writer (blocked by the reader)

    reader_entered = asyncio.Event()

    async def reader():
        async with lock.shared():
            reader_entered.set()

    r = asyncio.create_task(reader())
    await asyncio.sleep(0.02)
    assert not reader_entered.is_set()  # blocked behind the queued writer, not on the holder

    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w

    # The reader can now share with the still-present holder; the only wakeup available to it is
    # the cancelled writer abandoning its wait.
    await asyncio.wait_for(reader_entered.wait(), timeout=1.0)
    release_holder.set()
    await asyncio.gather(held, r)
