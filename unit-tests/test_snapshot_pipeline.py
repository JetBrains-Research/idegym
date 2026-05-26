"""Unit tests for run_snapshot_pipeline_job.

All Kubernetes operations, PodSnapshotService, and database helpers are
mocked so the test runs without a cluster or database.  The tests verify
that the pipeline calls the right functions in the right order, and that
the snapshot_prepare_requests counters are updated correctly on both the
success and failure paths.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from idegym.api.config import Config, NodePoolConfig, PodSnapshotConfig
from idegym.api.orchestrator.servers import ServerKind, StartServerRequest
from idegym.api.status import Status
from idegym.orchestrator.snapshot_pipeline import run_snapshot_pipeline_job

pytestmark = pytest.mark.unit

_IMAGE = "registry.example.com/server:latest"
_CLIENT_ID = uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config() -> Config:
    cfg = Config()
    cfg.orchestrator.pod_snapshot = PodSnapshotConfig(enabled=True, service_account_name="snap-sa")
    cfg.orchestrator.node_pool = NodePoolConfig(enabled=False)
    return cfg


def _request(**overrides) -> StartServerRequest:
    defaults = dict(
        client_id=_CLIENT_ID,
        image_tag=_IMAGE,
        server_name="snap-server",
        namespace="idegym",
        runtime_class_name="gvisor",
        run_as_root=False,
        service_port=80,
        container_port=8000,
        server_kind=ServerKind.IDEGYM,
        server_start_wait_timeout_in_seconds=30,
    )
    defaults.update(overrides)
    return StartServerRequest(**defaults)


def _patch_all(mocker, *, deploy_error=None):
    """Patch all external dependencies; optionally make deploy_server raise."""
    client = SimpleNamespace(name="test-client")
    server = SimpleNamespace(id=42, generated_name="snap-server-42", snapshot_name="snap-server-42")
    snapshot = SimpleNamespace(id=99)

    mocker.patch("idegym.orchestrator.snapshot_pipeline.validate_client", return_value=client)
    mocker.patch(
        "idegym.orchestrator.snapshot_pipeline.check_resources_and_save_server_in_db",
        return_value=server,
    )
    mocker.patch("idegym.orchestrator.snapshot_pipeline.extract_resources_request", return_value=(1.0, 2.0))

    if deploy_error:
        mocker.patch("idegym.orchestrator.snapshot_pipeline.deploy_server", side_effect=deploy_error)
    else:
        mocker.patch("idegym.orchestrator.snapshot_pipeline.deploy_server", return_value=None)

    mocker.patch("idegym.orchestrator.snapshot_pipeline.wait_for_pods_ready", return_value=None)
    mocker.patch("idegym.orchestrator.snapshot_pipeline.update_server_status", return_value=None)
    mocker.patch("idegym.orchestrator.snapshot_pipeline.clean_up_server", return_value=None)
    snapshot_svc = mocker.MagicMock()
    snapshot_svc.snapshot_server = mocker.AsyncMock(return_value="trigger-name")
    mocker.patch("idegym.orchestrator.snapshot_pipeline.PodSnapshotService", return_value=snapshot_svc)

    mocker.patch("idegym.orchestrator.snapshot_pipeline.create_snapshot", return_value=snapshot)

    update_job = mocker.patch("idegym.orchestrator.snapshot_pipeline.update_snapshot_job_status", return_value=None)
    update_succeeded = mocker.patch(
        "idegym.orchestrator.snapshot_pipeline.update_prepare_request_succeeded", return_value=None
    )
    update_failed = mocker.patch(
        "idegym.orchestrator.snapshot_pipeline.update_prepare_request_failed", return_value=None
    )

    return SimpleNamespace(
        server=server,
        snapshot=snapshot,
        update_job=update_job,
        update_succeeded=update_succeeded,
        update_failed=update_failed,
    )


# ===========================================================================
# Success path
# ===========================================================================


async def test_success_sets_job_status_to_success(mocker):
    mocks = _patch_all(mocker)
    job_id = str(uuid4())

    await run_snapshot_pipeline_job(job_id=job_id, request=_request(), config=_config())

    mocks.update_job.assert_awaited_once_with(
        job_id=job_id,
        status=Status.SUCCESS,
        snapshot_id=mocks.snapshot.id,
    )


async def test_success_increments_prepare_counter(mocker):
    mocks = _patch_all(mocker)
    prepare_id = uuid4()

    await run_snapshot_pipeline_job(
        job_id=str(uuid4()),
        request=_request(),
        config=_config(),
        prepare_request_id=prepare_id,
    )

    mocks.update_succeeded.assert_awaited_once_with(request_id=prepare_id)
    mocks.update_failed.assert_not_awaited()


async def test_success_without_prepare_id_skips_counter(mocker):
    mocks = _patch_all(mocker)

    await run_snapshot_pipeline_job(
        job_id=str(uuid4()),
        request=_request(),
        config=_config(),
        prepare_request_id=None,
    )

    mocks.update_succeeded.assert_not_awaited()
    mocks.update_failed.assert_not_awaited()


async def test_success_calls_snapshot_service(mocker):
    _patch_all(mocker)
    snapshot_svc_cls = mocker.patch("idegym.orchestrator.snapshot_pipeline.PodSnapshotService")
    instance = snapshot_svc_cls.return_value
    instance.snapshot_server = mocker.AsyncMock(return_value="trigger")

    await run_snapshot_pipeline_job(job_id=str(uuid4()), request=_request(), config=_config())

    instance.snapshot_server.assert_awaited_once()


# ===========================================================================
# Failure path
# ===========================================================================


async def test_failure_sets_job_status_to_failure(mocker):
    mocks = _patch_all(mocker, deploy_error=RuntimeError("k8s unreachable"))
    job_id = str(uuid4())

    await run_snapshot_pipeline_job(job_id=job_id, request=_request(), config=_config())

    mocks.update_job.assert_awaited_once_with(
        job_id=job_id,
        status=Status.FAILURE,
        details="k8s unreachable",
    )


async def test_failure_increments_failed_counter(mocker):
    mocks = _patch_all(mocker, deploy_error=RuntimeError("boom"))
    prepare_id = uuid4()

    await run_snapshot_pipeline_job(
        job_id=str(uuid4()),
        request=_request(),
        config=_config(),
        prepare_request_id=prepare_id,
    )

    mocks.update_failed.assert_awaited_once_with(request_id=prepare_id)
    mocks.update_succeeded.assert_not_awaited()


async def test_failure_without_prepare_id_skips_counter(mocker):
    mocks = _patch_all(mocker, deploy_error=RuntimeError("boom"))

    await run_snapshot_pipeline_job(
        job_id=str(uuid4()),
        request=_request(),
        config=_config(),
        prepare_request_id=None,
    )

    mocks.update_succeeded.assert_not_awaited()
    mocks.update_failed.assert_not_awaited()


async def test_failure_after_server_created_attempts_cleanup(mocker):
    _patch_all(mocker, deploy_error=RuntimeError("pod error"))
    cleanup = mocker.patch("idegym.orchestrator.snapshot_pipeline.clean_up_server", return_value=None)
    update_status = mocker.patch("idegym.orchestrator.snapshot_pipeline.update_server_status", return_value=None)

    await run_snapshot_pipeline_job(job_id=str(uuid4()), request=_request(), config=_config())

    cleanup.assert_awaited_once()
    update_status.assert_awaited()


async def test_failure_before_server_created_skips_cleanup(mocker):
    client = SimpleNamespace(name="test-client")
    mocker.patch("idegym.orchestrator.snapshot_pipeline.validate_client", return_value=client)
    mocker.patch("idegym.orchestrator.snapshot_pipeline.extract_resources_request", return_value=(1.0, 2.0))
    mocker.patch(
        "idegym.orchestrator.snapshot_pipeline.check_resources_and_save_server_in_db",
        side_effect=RuntimeError("resource limit exceeded"),
    )
    cleanup = mocker.patch("idegym.orchestrator.snapshot_pipeline.clean_up_server", return_value=None)
    mocker.patch("idegym.orchestrator.snapshot_pipeline.update_snapshot_job_status", return_value=None)
    mocker.patch("idegym.orchestrator.snapshot_pipeline.update_prepare_request_failed", return_value=None)

    await run_snapshot_pipeline_job(
        job_id=str(uuid4()),
        request=_request(),
        config=_config(),
        prepare_request_id=uuid4(),
    )

    cleanup.assert_not_awaited()
