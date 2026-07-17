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
                acquired = False
                try:
                    while self._writer or self._readers > 0:
                        await self._cond.wait()
                    acquired = True
                finally:
                    self._writers_waiting -= 1
                    if not acquired:
                        # A cancelled writer that abandons its wait must wake readers that blocked
                        # only because a writer was queued; otherwise they hang until the next
                        # unrelated release. The normal path becomes the writer and needs no notify.
                        self._cond.notify_all()
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


class _Entry:
    """A registry entry: the lock plus a reference count of its holders and waiters."""

    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = _RWLock()
        self.refs = 0


class ResourceScheduler:
    def __init__(self) -> None:
        self._locks: dict[str, _Entry] = {}
        self._guard = asyncio.Lock()

    async def _checkout(self, key: str) -> _Entry:
        """Get-or-create the entry for ``key`` and count this holder/waiter under the guard."""
        async with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = _Entry()
                self._locks[key] = entry
            entry.refs += 1
            return entry

    async def _checkin(self, key: str, entry: _Entry) -> None:
        """Drop this holder/waiter; remove the entry when nobody references it any more."""
        async with self._guard:
            entry.refs -= 1
            # Remove only when unreferenced and the mapping still points to this exact entry
            # (avoids an ABA race where a fresh entry replaced this key).
            if entry.refs <= 0 and self._locks.get(key) is entry:
                del self._locks[key]

    @asynccontextmanager
    async def acquire(self, requests: list[LockRequest]):
        """Acquire all ``requests`` in sorted key order, releasing in reverse (deadlock-free).

        Duplicate keys are merged (exclusive wins), so a caller never self-deadlocks by requesting
        the same key both shared and exclusive. The lock registry is reference-counted: a key's
        entry is created on first use (counting waiters too) and removed once no holder or waiter
        references it, so caller-controlled paths cannot grow the registry without bound.
        """
        merged: dict[str, bool] = {}
        for key, exclusive in requests:
            if not key:
                continue
            merged[key] = merged.get(key, False) or bool(exclusive)
        ordered = sorted(merged.items())
        checked_out: list[tuple[str, _Entry]] = []
        acquired: list[tuple[_Entry, bool]] = []
        try:
            for key, exclusive in ordered:
                entry = await self._checkout(key)
                checked_out.append((key, entry))
                await entry.lock.acquire(exclusive)
                acquired.append((entry, exclusive))
            yield
        finally:
            for entry, exclusive in reversed(acquired):
                await entry.lock.release(exclusive)
            for key, entry in reversed(checked_out):
                await self._checkin(key, entry)
