"""Async resource-lock scheduler with reader/writer semantics.

When calling OpenHands tools directly, the plugin must supply the locking the SDK execution layer
normally provides. Each tool declares a set of ``(key, exclusive)`` lock requests; a workspace
mutation takes the workspace key *exclusively* while contained file operations take it *shared* plus
an exclusive per-file lock, so a mutation conflicts with every contained file operation while
unrelated file operations still run in parallel. Locks are acquired in a deterministic key order to
stay deadlock-free.
"""

import asyncio
from contextlib import asynccontextmanager

# One lock request: the resource key and whether it must be held exclusively (a writer) or may be
# shared with other readers.
LockRequest = tuple[str, bool]


class _RWLock:
    """A writer-preferring async reader/writer lock.

    Multiple readers may hold it concurrently; a writer holds it alone. Writers are preferred (a new
    reader waits while a writer is queued) so a steady stream of readers cannot starve a writer.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    async def acquire(self, exclusive: bool) -> None:
        async with self._cond:
            if exclusive:
                self._writers_waiting += 1
                try:
                    while self._writer or self._readers > 0:
                        await self._cond.wait()
                finally:
                    self._writers_waiting -= 1
                self._writer = True
            else:
                while self._writer or self._writers_waiting > 0:
                    await self._cond.wait()
                self._readers += 1

    async def release(self, exclusive: bool) -> None:
        async with self._cond:
            if exclusive:
                self._writer = False
            else:
                self._readers -= 1
            self._cond.notify_all()


class ResourceScheduler:
    def __init__(self) -> None:
        self._locks: dict[str, _RWLock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, key: str) -> _RWLock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = _RWLock()
                self._locks[key] = lock
            return lock

    @asynccontextmanager
    async def acquire(self, requests: list[LockRequest]):
        """Acquire all ``requests`` in sorted key order, releasing in reverse (deadlock-free).

        Duplicate keys are merged (exclusive wins), so a caller never self-deadlocks by requesting
        the same key both shared and exclusive.
        """
        merged: dict[str, bool] = {}
        for key, exclusive in requests:
            if not key:
                continue
            merged[key] = merged.get(key, False) or bool(exclusive)
        ordered = sorted(merged.items())
        acquired: list[tuple[_RWLock, bool]] = []
        try:
            for key, exclusive in ordered:
                lock = await self._lock_for(key)
                await lock.acquire(exclusive)
                acquired.append((lock, exclusive))
            yield
        finally:
            for lock, exclusive in reversed(acquired):
                await lock.release(exclusive)
