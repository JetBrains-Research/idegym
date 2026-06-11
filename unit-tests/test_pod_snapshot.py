"""Unit tests for PodSnapshotService capturing the created PodSnapshot resource name.

The Kubernetes custom-objects client is mocked so no cluster is required.
"""

import pytest
from idegym.api.config import PodSnapshotConfig
from idegym.orchestrator.pod_snapshot import PodSnapshotService

pytestmark = pytest.mark.unit


def _triggered(name_payload: dict) -> dict:
    return {
        "status": {
            "conditions": [{"type": "Triggered", "status": "True", "reason": "Complete"}],
            **name_payload,
        }
    }


def test_created_snapshot_name_string():
    obj = _triggered({"snapshotCreated": "ps-uuid-0"})
    assert PodSnapshotService._created_snapshot_name(obj) == "ps-uuid-0"


def test_created_snapshot_name_nested():
    obj = _triggered({"snapshotCreated": {"name": "ps-uuid-1"}})
    assert PodSnapshotService._created_snapshot_name(obj) == "ps-uuid-1"


def test_created_snapshot_name_flat_fallback():
    obj = _triggered({"snapshotCreatedName": "ps-uuid-2"})
    assert PodSnapshotService._created_snapshot_name(obj) == "ps-uuid-2"


def test_created_snapshot_name_absent_returns_none():
    obj = _triggered({})
    assert PodSnapshotService._created_snapshot_name(obj) is None


def _patch_custom_get(mocker, obj):
    custom = mocker.MagicMock()
    custom.get_namespaced_custom_object = mocker.AsyncMock(return_value=obj)
    apps = batch = core = policy = mocker.MagicMock()
    mocker.patch(
        "idegym.backend.utils.kubernetes_client.create_clients",
        new=mocker.AsyncMock(return_value=(apps, batch, core, policy, custom)),
    )
    # Skip the post-completion GCS upload delay.
    mocker.patch("idegym.orchestrator.pod_snapshot.asyncio.sleep", new=mocker.AsyncMock(return_value=None))


async def test_wait_for_completion_returns_created_snapshot_name(mocker):
    _patch_custom_get(mocker, _triggered({"snapshotCreated": {"name": "ps-uuid-9"}}))
    service = PodSnapshotService(config=PodSnapshotConfig(), namespace="idegym")

    assert await service.wait_for_completion("trigger-x") == "ps-uuid-9"


async def test_wait_for_completion_returns_none_when_name_absent(mocker):
    _patch_custom_get(mocker, _triggered({}))
    service = PodSnapshotService(config=PodSnapshotConfig(), namespace="idegym")

    assert await service.wait_for_completion("trigger-x") is None
