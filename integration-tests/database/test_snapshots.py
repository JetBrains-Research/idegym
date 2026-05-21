"""Integration tests for snapshot database operations.

Covers snapshot_prepare_requests, snapshots, and snapshot_jobs tables against
a real PostgreSQL instance so that FK constraints, the LEFT JOIN in
get_snapshot_prepare_request_with_results, and atomic counter updates are
exercised faithfully.
"""

import hashlib
from uuid import uuid4

import pytest
from idegym.api.status import Status
from idegym.orchestrator.database.database import (
    find_snapshot_by_request_hash,
    get_snapshot_job_with_name,
    get_snapshot_prepare_request,
    get_snapshot_prepare_request_with_results,
    increment_snapshot_prepare_failed,
    increment_snapshot_prepare_succeeded,
    save_snapshot,
    save_snapshot_job,
    save_snapshot_prepare_request,
    update_snapshot_job,
)
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_NAMESPACE = "idegym"
_IMAGE = "registry.example.com/server:latest"


def _hash(salt: str) -> str:
    return hashlib.sha256(f"test-{salt}".encode()).hexdigest()


async def _snapshot(db: AsyncSession, name: str, request_hash: str) -> object:
    return await save_snapshot(
        db,
        snapshot_name=name,
        request_hash=request_hash,
        namespace=_NAMESPACE,
        image_tag=_IMAGE,
        server_name="test-server",
        runtime_class_name="gvisor",
        run_as_root=False,
        server_kind="idegym",
    )


# ===========================================================================
# snapshots table
# ===========================================================================


async def test_save_and_find_snapshot(db: AsyncSession):
    h = _hash("a")
    snap = await _snapshot(db, "server-1", h)
    assert snap.id is not None

    found = await find_snapshot_by_request_hash(db, h)
    assert found is not None
    assert found.snapshot_name == "server-1"


async def test_find_snapshot_returns_most_recent(db: AsyncSession):
    h = _hash("b")
    await _snapshot(db, "old", h)
    await _snapshot(db, "new", h)

    found = await find_snapshot_by_request_hash(db, h)
    assert found.snapshot_name == "new"


async def test_find_snapshot_returns_none_for_unknown_hash(db: AsyncSession):
    assert await find_snapshot_by_request_hash(db, _hash("missing")) is None


# ===========================================================================
# snapshot_prepare_requests table
# ===========================================================================


async def test_save_and_get_snapshot_prepare_request(db: AsyncSession):
    req_id = uuid4()
    record = await save_snapshot_prepare_request(db, request_id=req_id, total_requested=3)
    assert record.id == req_id
    assert record.total_requested == 3
    assert record.succeeded == 0
    assert record.failed == 0

    fetched = await get_snapshot_prepare_request(db, req_id)
    assert fetched is not None
    assert fetched.id == req_id


async def test_get_snapshot_prepare_request_returns_none_for_unknown(db: AsyncSession):
    assert await get_snapshot_prepare_request(db, uuid4()) is None


async def test_increment_succeeded(db: AsyncSession):
    req_id = uuid4()
    await save_snapshot_prepare_request(db, request_id=req_id, total_requested=2)

    await increment_snapshot_prepare_succeeded(db, request_id=req_id)
    await increment_snapshot_prepare_succeeded(db, request_id=req_id)

    record = await get_snapshot_prepare_request(db, req_id)
    assert record.succeeded == 2
    assert record.failed == 0


async def test_increment_failed(db: AsyncSession):
    req_id = uuid4()
    await save_snapshot_prepare_request(db, request_id=req_id, total_requested=2)

    await increment_snapshot_prepare_failed(db, request_id=req_id)

    record = await get_snapshot_prepare_request(db, req_id)
    assert record.failed == 1
    assert record.succeeded == 0


async def test_independent_counters(db: AsyncSession):
    req_id = uuid4()
    await save_snapshot_prepare_request(db, request_id=req_id, total_requested=4)

    await increment_snapshot_prepare_succeeded(db, request_id=req_id)
    await increment_snapshot_prepare_failed(db, request_id=req_id)
    await increment_snapshot_prepare_failed(db, request_id=req_id)

    record = await get_snapshot_prepare_request(db, req_id)
    assert record.succeeded == 1
    assert record.failed == 2


# ===========================================================================
# snapshot_jobs table
# ===========================================================================


async def test_save_snapshot_job_linked_to_prepare_request(db: AsyncSession):
    req_id = uuid4()
    await save_snapshot_prepare_request(db, request_id=req_id, total_requested=1)

    job_id = str(uuid4())
    job = await save_snapshot_job(db, job_id=job_id, request_hash=_hash("j1"), request="{}", prepare_request_id=req_id)
    assert job.status == Status.IN_PROGRESS
    assert job.prepare_request_id == req_id


async def test_save_snapshot_job_without_prepare_request(db: AsyncSession):
    job_id = str(uuid4())
    job = await save_snapshot_job(db, job_id=job_id, request_hash=_hash("standalone"), request="{}")
    assert job.prepare_request_id is None


async def test_get_snapshot_job_with_name_before_snapshot(db: AsyncSession):
    req_id = uuid4()
    await save_snapshot_prepare_request(db, request_id=req_id, total_requested=1)
    job_id = str(uuid4())
    await save_snapshot_job(db, job_id=job_id, request_hash=_hash("j2"), request="{}", prepare_request_id=req_id)

    result = await get_snapshot_job_with_name(db, job_id)
    assert result is not None
    job, snapshot_name = result
    assert job.job_id == job_id
    assert snapshot_name is None


async def test_get_snapshot_job_with_name_after_success(db: AsyncSession):
    req_id = uuid4()
    await save_snapshot_prepare_request(db, request_id=req_id, total_requested=1)
    h = _hash("j3")
    job_id = str(uuid4())
    await save_snapshot_job(db, job_id=job_id, request_hash=h, request="{}", prepare_request_id=req_id)

    snap = await _snapshot(db, "server-99", h)
    await update_snapshot_job(db, job_id=job_id, status=Status.SUCCESS, snapshot_id=snap.id)

    result = await get_snapshot_job_with_name(db, job_id)
    assert result is not None
    job, snapshot_name = result
    assert job.status == Status.SUCCESS
    assert snapshot_name == "server-99"


# ===========================================================================
# get_snapshot_prepare_request_with_results: the core joined query
# ===========================================================================


async def test_prepare_request_with_results_returns_none_for_unknown(db: AsyncSession):
    assert await get_snapshot_prepare_request_with_results(db, uuid4()) is None


async def test_prepare_request_with_results_all_in_progress(db: AsyncSession):
    req_id = uuid4()
    await save_snapshot_prepare_request(db, request_id=req_id, total_requested=2)
    h1, h2 = _hash("p1"), _hash("p2")
    await save_snapshot_job(db, job_id=str(uuid4()), request_hash=h1, request="{}", prepare_request_id=req_id)
    await save_snapshot_job(db, job_id=str(uuid4()), request_hash=h2, request="{}", prepare_request_id=req_id)

    prepare, results = await get_snapshot_prepare_request_with_results(db, req_id)
    assert prepare.total_requested == 2
    assert len(results) == 2
    assert all(r[1] == Status.IN_PROGRESS for r in results)
    assert all(r[2] is None for r in results)  # no snapshot_name yet


async def test_prepare_request_with_results_mixed(db: AsyncSession):
    req_id = uuid4()
    await save_snapshot_prepare_request(db, request_id=req_id, total_requested=3)
    h1, h2, h3 = _hash("m1"), _hash("m2"), _hash("m3")
    job_a, job_b, job_c = str(uuid4()), str(uuid4()), str(uuid4())

    for job_id, h in [(job_a, h1), (job_b, h2), (job_c, h3)]:
        await save_snapshot_job(db, job_id=job_id, request_hash=h, request="{}", prepare_request_id=req_id)

    # job_a succeeds
    snap = await _snapshot(db, "server-100", h1)
    await update_snapshot_job(db, job_id=job_a, status=Status.SUCCESS, snapshot_id=snap.id)
    await increment_snapshot_prepare_succeeded(db, request_id=req_id)

    # job_b fails
    await update_snapshot_job(db, job_id=job_b, status=Status.FAILURE, details="k8s error")
    await increment_snapshot_prepare_failed(db, request_id=req_id)

    # job_c still in progress

    prepare, results = await get_snapshot_prepare_request_with_results(db, req_id)
    assert prepare.succeeded == 1
    assert prepare.failed == 1

    by_hash = {r[0]: r for r in results}

    ra = by_hash[h1]
    assert ra[1] == Status.SUCCESS
    assert ra[2] == "server-100"
    assert ra[3] is None

    rb = by_hash[h2]
    assert rb[1] == Status.FAILURE
    assert rb[2] is None
    assert rb[3] == "k8s error"

    rc = by_hash[h3]
    assert rc[1] == Status.IN_PROGRESS
    assert rc[2] is None


async def test_prepare_request_with_results_empty_batch(db: AsyncSession):
    req_id = uuid4()
    await save_snapshot_prepare_request(db, request_id=req_id, total_requested=0)

    prepare, results = await get_snapshot_prepare_request_with_results(db, req_id)
    assert prepare.total_requested == 0
    assert results == []


async def test_prepare_request_results_only_include_own_jobs(db: AsyncSession):
    req_a, req_b = uuid4(), uuid4()
    await save_snapshot_prepare_request(db, request_id=req_a, total_requested=1)
    await save_snapshot_prepare_request(db, request_id=req_b, total_requested=1)
    await save_snapshot_job(db, job_id=str(uuid4()), request_hash=_hash("qa"), request="{}", prepare_request_id=req_a)
    await save_snapshot_job(db, job_id=str(uuid4()), request_hash=_hash("qb"), request="{}", prepare_request_id=req_b)

    _, results_a = await get_snapshot_prepare_request_with_results(db, req_a)
    _, results_b = await get_snapshot_prepare_request_with_results(db, req_b)

    assert len(results_a) == 1
    assert len(results_b) == 1
    assert results_a[0][0] == _hash("qa")
    assert results_b[0][0] == _hash("qb")
