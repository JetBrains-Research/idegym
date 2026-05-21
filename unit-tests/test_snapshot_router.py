"""Unit tests for the snapshot router endpoints.

All database helpers and the pipeline function are mocked so tests run
without a database or Kubernetes cluster.  Tests are called directly as
async functions (same pattern as test_orchestrator_forwarding.py) rather
than through an HTTP test client.
"""

import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from idegym.api.config import Config, PodSnapshotConfig
from idegym.api.orchestrator.servers import ServerKind, StartServerRequest
from idegym.api.orchestrator.snapshots import PrepareSnapshotsRequest, SnapshotExistsRequest
from idegym.api.status import Status
from idegym.orchestrator.router.snapshot import (
    check_snapshot_exists,
    get_prepare_snapshots_status,
    get_snapshot_job_status,
    prepare_snapshots,
)
from starlette.requests import Request

pytestmark = pytest.mark.unit

_IMAGE = "registry.example.com/server:latest"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash(salt: str) -> str:
    return hashlib.sha256(f"test-{salt}".encode()).hexdigest()


def _config(enabled: bool = True) -> Config:
    cfg = Config()
    cfg.orchestrator.pod_snapshot = PodSnapshotConfig(enabled=enabled)
    return cfg


def _low_level_request(config: Config) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/snapshots/prepare",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 8000),
            "client": ("testclient", 50000),
            "app": SimpleNamespace(state=SimpleNamespace(config=config)),
        }
    )


def _start_request(client_id: UUID = None) -> StartServerRequest:
    return StartServerRequest(
        client_id=client_id or uuid4(),
        image_tag=_IMAGE,
        server_name="snap-server",
        namespace="idegym",
        runtime_class_name="gvisor",
        server_kind=ServerKind.IDEGYM,
    )


# ===========================================================================
# POST /api/snapshots/prepare
# ===========================================================================


async def test_prepare_snapshots_returns_request_id(mocker):
    mocker.patch("idegym.orchestrator.router.snapshot.create_snapshot_prepare_request", return_value=None)
    mocker.patch("idegym.orchestrator.router.snapshot.create_snapshot_job", return_value=None)
    mocker.patch("idegym.orchestrator.router.snapshot.run_snapshot_pipeline_job")
    mocker.patch("asyncio.create_task")

    response = await prepare_snapshots(
        request=PrepareSnapshotsRequest(requests=[_start_request()]),
        low_level_request=_low_level_request(_config()),
    )

    UUID(response.request_id)  # raises ValueError if not a valid UUID


async def test_prepare_snapshots_creates_one_job_per_request(mocker):
    mocker.patch("idegym.orchestrator.router.snapshot.create_snapshot_prepare_request", return_value=None)
    create_job = mocker.patch("idegym.orchestrator.router.snapshot.create_snapshot_job", return_value=None)
    mocker.patch("idegym.orchestrator.router.snapshot.run_snapshot_pipeline_job")
    mocker.patch("asyncio.create_task")

    await prepare_snapshots(
        request=PrepareSnapshotsRequest(requests=[_start_request(), _start_request(), _start_request()]),
        low_level_request=_low_level_request(_config()),
    )

    assert create_job.await_count == 3


async def test_prepare_snapshots_passes_total_requested_to_prepare_record(mocker):
    create_prepare = mocker.patch(
        "idegym.orchestrator.router.snapshot.create_snapshot_prepare_request", return_value=None
    )
    mocker.patch("idegym.orchestrator.router.snapshot.create_snapshot_job", return_value=None)
    mocker.patch("idegym.orchestrator.router.snapshot.run_snapshot_pipeline_job")
    mocker.patch("asyncio.create_task")

    requests = [_start_request(), _start_request()]
    await prepare_snapshots(
        request=PrepareSnapshotsRequest(requests=requests),
        low_level_request=_low_level_request(_config()),
    )

    assert create_prepare.call_args.kwargs["total_requested"] == 2


async def test_prepare_snapshots_passes_prepare_request_id_to_each_job(mocker):
    mocker.patch("idegym.orchestrator.router.snapshot.create_snapshot_prepare_request", return_value=None)
    create_job = mocker.patch("idegym.orchestrator.router.snapshot.create_snapshot_job", return_value=None)
    mocker.patch("idegym.orchestrator.router.snapshot.run_snapshot_pipeline_job")
    mocker.patch("asyncio.create_task")

    await prepare_snapshots(
        request=PrepareSnapshotsRequest(requests=[_start_request(), _start_request()]),
        low_level_request=_low_level_request(_config()),
    )

    # Both jobs must receive the same prepare_request_id (a UUID)
    ids = {call.kwargs["prepare_request_id"] for call in create_job.await_args_list}
    assert len(ids) == 1
    assert isinstance(next(iter(ids)), UUID)


async def test_prepare_snapshots_raises_400_when_feature_disabled(mocker):
    with pytest.raises(HTTPException) as exc_info:
        await prepare_snapshots(
            request=PrepareSnapshotsRequest(requests=[_start_request()]),
            low_level_request=_low_level_request(_config(enabled=False)),
        )
    assert exc_info.value.status_code == 400


# ===========================================================================
# GET /api/snapshots/prepare/{request_id}
# ===========================================================================


async def test_get_prepare_status_in_progress(mocker):
    prepare = SimpleNamespace(total_requested=3, succeeded=1, failed=0)
    job_results = [
        (_hash("a"), Status.SUCCESS, "server-1", None),
        (_hash("b"), Status.IN_PROGRESS, None, None),
        (_hash("c"), Status.IN_PROGRESS, None, None),
    ]
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_prepare_request_with_results",
        return_value=(prepare, job_results),
    )

    response = await get_prepare_snapshots_status(request_id=str(uuid4()))

    assert response.status == "IN_PROGRESS"
    assert response.results is None  # withheld until READY


async def test_get_prepare_status_ready_when_all_done(mocker):
    prepare = SimpleNamespace(total_requested=2, succeeded=1, failed=1)
    h1, h2 = _hash("x"), _hash("y")
    job_results = [
        (h1, Status.SUCCESS, "server-10", None),
        (h2, Status.FAILURE, None, "deploy failed"),
    ]
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_prepare_request_with_results",
        return_value=(prepare, job_results),
    )

    response = await get_prepare_snapshots_status(request_id=str(uuid4()))

    assert response.status == "READY"
    assert response.total_requested == 2
    assert response.succeeded == 1
    assert response.failed == 1
    assert response.results is not None
    assert len(response.results) == 2


async def test_get_prepare_status_ready_results_map_hash_to_outcome(mocker):
    prepare = SimpleNamespace(total_requested=2, succeeded=1, failed=1)
    h1, h2 = _hash("r1"), _hash("r2")
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_prepare_request_with_results",
        return_value=(
            prepare,
            [
                (h1, Status.SUCCESS, "server-77", None),
                (h2, Status.FAILURE, None, "timeout"),
            ],
        ),
    )

    response = await get_prepare_snapshots_status(request_id=str(uuid4()))

    by_hash = {r.request_hash: r for r in response.results}
    assert by_hash[h1].snapshot_name == "server-77"
    assert by_hash[h1].details is None
    assert by_hash[h2].snapshot_name is None
    assert by_hash[h2].details == "timeout"


async def test_get_prepare_status_returns_404_for_unknown(mocker):
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_prepare_request_with_results",
        return_value=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        await get_prepare_snapshots_status(request_id=str(uuid4()))
    assert exc_info.value.status_code == 404


async def test_get_prepare_status_returns_400_for_invalid_uuid():
    with pytest.raises(HTTPException) as exc_info:
        await get_prepare_snapshots_status(request_id="not-a-uuid")
    assert exc_info.value.status_code == 400


async def test_get_prepare_status_ready_when_all_failed(mocker):
    prepare = SimpleNamespace(total_requested=2, succeeded=0, failed=2)
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_prepare_request_with_results",
        return_value=(
            prepare,
            [
                (_hash("f1"), Status.FAILURE, None, "err1"),
                (_hash("f2"), Status.FAILURE, None, "err2"),
            ],
        ),
    )

    response = await get_prepare_snapshots_status(request_id=str(uuid4()))
    assert response.status == "READY"
    assert response.succeeded == 0
    assert response.failed == 2
    assert all(r.snapshot_name is None for r in response.results)


# ===========================================================================
# GET /api/snapshots/jobs/{job_id}
# ===========================================================================


async def test_get_snapshot_job_status_success(mocker):
    job_id = str(uuid4())
    job = SimpleNamespace(job_id=job_id, status=Status.SUCCESS, details=None)
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_job_with_name",
        return_value=(job, "server-42"),
    )

    response = await get_snapshot_job_status(job_id=job_id)

    assert response.job_id == job_id
    assert response.status == Status.SUCCESS
    assert response.snapshot_name == "server-42"
    assert response.details is None


async def test_get_snapshot_job_status_failure_with_details(mocker):
    job_id = str(uuid4())
    job = SimpleNamespace(job_id=job_id, status=Status.FAILURE, details="pod crash")
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_job_with_name",
        return_value=(job, None),
    )

    response = await get_snapshot_job_status(job_id=job_id)

    assert response.status == Status.FAILURE
    assert response.snapshot_name is None
    assert response.details == "pod crash"


async def test_get_snapshot_job_status_returns_404_for_unknown(mocker):
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_job_with_name",
        return_value=None,
    )
    with pytest.raises(HTTPException) as exc_info:
        await get_snapshot_job_status(job_id="unknown-job")
    assert exc_info.value.status_code == 404


# ===========================================================================
# GET /api/snapshots/exists
# ===========================================================================


async def test_check_snapshot_exists_found(mocker):
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_for_request",
        return_value=SimpleNamespace(snapshot_name="server-77"),
    )

    response = await check_snapshot_exists(
        request=SnapshotExistsRequest(namespace="idegym", image_tag=_IMAGE, server_name="my-server")
    )

    assert response.exists is True
    assert response.snapshot_name == "server-77"


async def test_check_snapshot_exists_not_found(mocker):
    mocker.patch(
        "idegym.orchestrator.router.snapshot.find_snapshot_for_request",
        return_value=None,
    )

    response = await check_snapshot_exists(
        request=SnapshotExistsRequest(namespace="idegym", image_tag=_IMAGE, server_name="my-server")
    )

    assert response.exists is False
    assert response.snapshot_name is None
