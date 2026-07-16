"""Unit tests for the request deduplicator (OH-09)."""

import asyncio

import pytest
from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.runtime.dedup import RequestDeduplicator, canonical_hash

pytestmark = pytest.mark.unit


def test_canonical_hash_ignores_nested_key_order():
    a = canonical_hash({"tool": "grep", "arguments": {"a": 1, "b": {"x": 1, "y": 2}}})
    b = canonical_hash({"arguments": {"b": {"y": 2, "x": 1}, "a": 1}, "tool": "grep"})
    assert a == b  # nested dict reordering is not a different request
    # the tool name is part of the key: distinct tools never collide
    assert canonical_hash({"tool": "grep"}) != canonical_hash({"tool": "glob"})


async def test_single_flight_runs_factory_once():
    dedup = RequestDeduplicator(10)
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def factory():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"n": calls}

    h = canonical_hash({"x": 1})
    t1 = asyncio.create_task(dedup.run("r1", h, factory))
    await started.wait()
    t2 = asyncio.create_task(dedup.run("r1", h, factory))  # follower: must not execute again
    await asyncio.sleep(0.01)
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert calls == 1 and r1 == r2 == {"n": 1}


async def test_reused_id_with_different_body_conflicts():
    dedup = RequestDeduplicator(10)

    async def f():
        return "ok"

    await dedup.run("r1", canonical_hash({"a": 1}), f)
    with pytest.raises(ServiceError) as exc:
        await dedup.run("r1", canonical_hash({"a": 2}), f)
    assert exc.value.code == ErrorCode.DUPLICATE_REQUEST_ID


async def test_failure_is_not_cached_and_retry_reruns():
    dedup = RequestDeduplicator(10)
    calls = 0

    async def f():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return "ok"

    h = canonical_hash({"a": 1})
    with pytest.raises(RuntimeError):
        await dedup.run("r1", h, f)
    # a later retry with the same id re-runs (the failure was not cached as a success)
    assert await dedup.run("r1", h, f) == "ok"
    assert calls == 2


async def test_concurrent_followers_share_a_single_failure():
    dedup = RequestDeduplicator(10)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def f():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        raise RuntimeError("boom")

    h = canonical_hash({"a": 1})
    t1 = asyncio.create_task(dedup.run("r1", h, f))
    await started.wait()
    t2 = asyncio.create_task(dedup.run("r1", h, f))
    await asyncio.sleep(0.01)
    release.set()
    results = await asyncio.gather(t1, t2, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert calls == 1  # single-flight even when the leader fails
