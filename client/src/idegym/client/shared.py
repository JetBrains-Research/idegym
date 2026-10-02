"""Drive one IdeGYM registration from more than one thread or event loop.

An :class:`~idegym.client.client.IdeGYMClient` is bound to the loop that created it, so a caller
driving sandboxes from several loops cannot share it directly. :class:`SharedIdeGYMClient` owns a
loop in a dedicated thread, registers the client there, and marshals every call onto it.

Sharing one registration matters: deregistering terminates every server the client owns, so with
two registrations per process one could tear down the other's sandboxes.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any, Optional, Self

from idegym.client.client import IdeGYMClient
from idegym.utils.logging import get_logger

logger = get_logger(__name__)


class _LoopThread:
    """An event loop running in its own daemon thread, accepting work from any other thread."""

    # Drain phases: pending work finishing on its own, cancelled tasks unwinding, shutdown hooks.
    # close() waits for all three plus a margin, so it cannot give up mid-drain.
    _DRAIN_TIMEOUT_SECONDS = 5.0
    _CANCEL_TIMEOUT_SECONDS = 2.0
    _SHUTDOWN_HOOKS_TIMEOUT_SECONDS = 5.0
    _CLOSE_MARGIN_SECONDS = 1.0

    def __init__(self, name: str) -> None:
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._closing = False
        self._closing_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()
        # Block until the loop is actually running, so the first submit cannot race the start.
        self._started.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._started.set)
        self._loop.run_forever()

    def submit[T](self, factory: Callable[[], Awaitable[T]]) -> Future[T]:
        """Run ``factory()`` on the owned loop and return a future the calling thread can wait on.

        A factory rather than a coroutine so that the awaitable is created on the owning loop:
        anything a coroutine function touches at creation time then belongs to the right loop.

        Refused once :meth:`close` has started: work scheduled after the drain took stock would
        never run, and a caller waiting on it without a timeout would hang.
        """
        with self._closing_lock:
            if self._closing:
                raise RuntimeError("The IdeGYM client loop is shutting down; it accepts no new work")
            return self._schedule(factory)

    def _schedule[T](self, factory: Callable[[], Awaitable[T]]) -> Future[T]:
        async def call() -> T:
            return await factory()

        return asyncio.run_coroutine_threadsafe(call(), self._loop)

    def close(self) -> None:
        """Drain the loop before stopping it, so nothing is torn down mid-flight.

        ``IdeGYMClient.__aexit__`` cancels the heartbeat task without awaiting it, and the file
        transfers hand work to ``asyncio.to_thread``. Stopping the loop immediately would leave
        those pending — Python then prints "Task was destroyed but it is pending!" on every exit,
        and the executor and async generators never get their shutdown hooks.
        """
        with self._closing_lock:
            self._closing = True
            drain = self._schedule(self._drain)
        close_timeout = (
            self._DRAIN_TIMEOUT_SECONDS
            + self._CANCEL_TIMEOUT_SECONDS
            + self._SHUTDOWN_HOOKS_TIMEOUT_SECONDS
            + self._CLOSE_MARGIN_SECONDS
        )
        try:
            drain.result(timeout=close_timeout)
        # A failed or slow drain must not prevent shutdown; a KeyboardInterrupt still propagates.
        except Exception:
            logger.debug("Loop drain did not finish cleanly; stopping anyway", exc_info=True)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._loop.close()

    async def _drain(self) -> None:
        """Let pending work finish, cancel what does not, then run the loop's own shutdown hooks.

        Cancelling is what releases a thread blocked on a :meth:`submit` future with no timeout.
        """
        pending = [task for task in asyncio.all_tasks(self._loop) if task is not asyncio.current_task()]
        if pending:
            _, still_pending = await asyncio.wait(pending, timeout=self._DRAIN_TIMEOUT_SECONDS)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.wait(still_pending, timeout=self._CANCEL_TIMEOUT_SECONDS)
        await self._loop.shutdown_asyncgens()
        await self._loop.shutdown_default_executor(timeout=self._SHUTDOWN_HOOKS_TIMEOUT_SECONDS)


class SharedIdeGYMClient:
    """A thread-safe handle on one registered :class:`IdeGYMClient`.

    Takes the same arguments as ``IdeGYMClient`` and registers on entry, exactly as
    ``async with IdeGYMClient(...)`` would::

        with SharedIdeGYMClient(orchestrator_url=..., name="run", namespace="idegym") as shared:
            server = shared.run(lambda client: client.start_server(image_tag=...))
            result = shared.run(lambda _: server.execute_bash("echo hi"))
            shared.run(lambda client: client.stop_server(server))

    Every call is marshalled onto the owned loop, so concurrent calls from different threads are
    safe and all of them share the one registration.
    """

    def __init__(self, **client_arguments: Any) -> None:
        self._client_arguments = client_arguments
        self._loop: Optional[_LoopThread] = None
        self._client: Optional[IdeGYMClient] = None

    @property
    def client(self) -> IdeGYMClient:
        """The underlying client. Only touch it from a callable passed to :meth:`run`."""
        if self._client is None:
            raise RuntimeError("SharedIdeGYMClient is not started; use it as a context manager")
        return self._client

    def __enter__(self) -> Self:
        self._loop = _LoopThread(name="idegym-client-loop")

        async def start() -> IdeGYMClient:
            client = IdeGYMClient(**self._client_arguments)
            await client.__aenter__()
            return client

        try:
            self._client = self._loop.submit(start).result()
        except BaseException:
            self._loop.close()
            self._loop = None
            raise
        return self

    def __exit__(self, exception_type, exception, traceback) -> bool:
        loop, client = self._loop, self._client
        self._client = None
        self._loop = None
        try:
            if client is not None and loop is not None:
                loop.submit(lambda: client.__aexit__(exception_type, exception, traceback)).result()
        finally:
            if loop is not None:
                loop.close()
        return False

    def submit[T](self, call: Callable[[IdeGYMClient], Awaitable[T]]) -> Future[T]:
        """Schedule ``call(client)`` on the owned loop without waiting for it."""
        if self._loop is None:
            raise RuntimeError("SharedIdeGYMClient is not started; use it as a context manager")
        client = self.client
        return self._loop.submit(lambda: call(client))

    def run[T](self, call: Callable[[IdeGYMClient], Awaitable[T]], timeout: Optional[float] = None) -> T:
        """Run ``call(client)`` on the owned loop and return its result.

        Safe to call from any thread, including one that is itself running an event loop — the
        awaitable never touches the caller's loop.

        If ``timeout`` expires, the call is cancelled before ``TimeoutError`` is raised, so that,
        say, a timed-out ``start_server`` does not go on to create a server nobody holds.
        """
        future = self.submit(call)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise
