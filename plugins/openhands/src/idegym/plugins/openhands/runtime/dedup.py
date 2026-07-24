"""Request deduplication with single-flight execution.

A caller may attach a ``request_id`` to a tool call so an at-least-once retry does not run the tool's
side effects twice. Correctness requires:

* the dedup key covers the *operation* (kind, tool, terminal, environment generation, arguments) via
  canonical JSON, so a reused id never returns a different tool's result and nested-dict key order
  does not matter;
* the operation is validated (tool exists + enabled) *before* the cache is consulted;
* concurrent identical ids are single-flight — the first executes, the rest await its outcome — so
  side effects run exactly once;
* failures/cancellations are not cached as successes: in-flight followers observe the same outcome,
  but a later retry with the same id re-runs.
"""

import asyncio
import hashlib
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError


def canonical_hash(payload: dict[str, Any]) -> str:
    """A stable hash of ``payload`` independent of dict key order (recursively sorted)."""
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class _Entry:
    __slots__ = ("done", "error", "hash", "result")

    def __init__(self, body_hash: str) -> None:
        self.hash = body_hash
        self.done = asyncio.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class RequestDeduplicator:
    def __init__(self, max_size: int) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max = max(1, max_size)

    async def run(self, request_id: str, body_hash: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Run ``factory`` at most once per (request_id, body_hash); followers await the outcome."""
        async with self._lock:
            entry = self._entries.get(request_id)
            if entry is not None:
                if entry.hash != body_hash:
                    raise ServiceError(
                        ErrorCode.DUPLICATE_REQUEST_ID,
                        f"request_id {request_id!r} was already used with a different request",
                    )
                if entry.done.is_set():
                    self._entries.move_to_end(request_id)
                    if entry.error is not None:
                        raise entry.error
                    return entry.result
                follower = entry
                leader = False
            else:
                entry = _Entry(body_hash)
                self._entries[request_id] = entry
                follower = entry
                leader = True

        if not leader:
            await follower.done.wait()
            if follower.error is not None:
                raise follower.error
            return follower.result

        try:
            result = await factory()
        except BaseException as exc:
            # Do not cache a failure/cancellation as a success: drop the entry so a later retry with
            # the same id re-runs, but still hand the outcome to any in-flight followers.
            async with self._lock:
                self._entries.pop(request_id, None)
            # Followers are independent tasks that were NOT cancelled. Re-raising the leader's
            # CancelledError inside them would surface as an unhandled error (a 500) rather than a
            # clean, retryable outcome and would corrupt their task state. Give followers a
            # retryable service error while the leader still re-raises its own cancellation.
            if isinstance(exc, asyncio.CancelledError):
                entry.error = ServiceError(
                    ErrorCode.SERVICE_UNAVAILABLE,
                    "the in-flight request was cancelled before completion; retry with the same request_id",
                )
            else:
                entry.error = exc
            entry.done.set()
            raise
        async with self._lock:
            entry.result = result
            entry.done.set()
            self._entries.move_to_end(request_id)
            self._evict()
        return result

    def _evict(self) -> None:
        # Drop the oldest *completed* entries; never evict an in-flight entry (its followers would
        # then re-execute the side effect).
        while len(self._entries) > self._max:
            for rid, entry in self._entries.items():
                if entry.done.is_set():
                    del self._entries[rid]
                    break
            else:
                break

    def clear(self) -> None:
        self._entries.clear()
