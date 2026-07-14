"""Async resource-lock scheduler.

When calling OpenHands tools directly, the plugin must supply the locking normally provided by the
SDK execution layer. Locks are acquired in a deterministic sorted order to avoid deadlocks.
"""

import asyncio
from contextlib import asynccontextmanager


class ResourceScheduler:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    @asynccontextmanager
    async def acquire(self, keys: list[str]):
        """Acquire all ``keys`` in sorted order, releasing in reverse (deadlock-free)."""
        ordered = sorted(set(k for k in keys if k))
        acquired: list[asyncio.Lock] = []
        try:
            for key in ordered:
                lock = await self._lock_for(key)
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
