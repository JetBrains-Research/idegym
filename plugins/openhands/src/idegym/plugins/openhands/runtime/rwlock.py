"""A writer-preferring async reader/writer lock exposed as shared()/exclusive() context managers.

Used as the environment lease: normal tool calls and terminal operations hold it *shared*, while a
reset or stop holds it *exclusive*. The exclusive holder therefore blocks until every in-flight
operation has drained, and no new operation starts until the reset completes.
"""

import asyncio
from contextlib import asynccontextmanager


class RWLock:
    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    async def _acquire(self, exclusive: bool) -> None:
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
                # Writer preference: a new reader waits while a writer is queued, so a steady stream
                # of readers cannot starve a pending reset.
                while self._writer or self._writers_waiting > 0:
                    await self._cond.wait()
                self._readers += 1

    async def _release(self, exclusive: bool) -> None:
        async with self._cond:
            if exclusive:
                self._writer = False
            else:
                self._readers -= 1
            self._cond.notify_all()

    @asynccontextmanager
    async def shared(self):
        await self._acquire(False)
        try:
            yield
        finally:
            await self._release(False)

    @asynccontextmanager
    async def exclusive(self):
        await self._acquire(True)
        try:
            yield
        finally:
            await self._release(True)
