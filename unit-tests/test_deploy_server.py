"""Unit tests for deploy_server snapshot label/annotation wiring.

All Kubernetes clients are mocked so the deployment body can be inspected without a cluster.
"""

from types import SimpleNamespace

import pytest
from idegym.backend.utils.kubernetes_client import deploy_server

pytestmark = pytest.mark.unit

PS_NAME_ANNOTATION = "podsnapshot.gke.io/ps-name"
SNAPSHOT_ID_LABEL = "idegym.jetbrains.com/snapshot-id"


def _mock_clients(mocker):
    apps = mocker.MagicMock()
    core = mocker.MagicMock()
    policy = mocker.MagicMock()
    custom = mocker.MagicMock()
    batch = mocker.MagicMock()

    created_deployment = SimpleNamespace(
        api_version="apps/v1",
        kind="Deployment",
        metadata=SimpleNamespace(name="srv", uid="uid-1"),
    )
    apps.create_namespaced_deployment = mocker.AsyncMock(return_value=created_deployment)
    core.create_namespaced_service = mocker.AsyncMock(return_value=None)
    policy.create_namespaced_pod_disruption_budget = mocker.AsyncMock(return_value=None)

    mocker.patch(
        "idegym.backend.utils.kubernetes_client.create_clients",
        new=mocker.AsyncMock(return_value=(apps, batch, core, policy, custom)),
    )
    return apps


async def _deploy_pod_meta(mocker, **kwargs):
    apps = _mock_clients(mocker)
    await deploy_server(image_tag="img:latest", server_name="srv", namespace="idegym", **kwargs)
    body = apps.create_namespaced_deployment.await_args.kwargs["body"]
    return body.spec.template.metadata


async def test_no_snapshot_tag_omits_ps_name_annotation(mocker):
    meta = await _deploy_pod_meta(mocker)
    assert PS_NAME_ANNOTATION not in meta.annotations
    # snapshot-id label falls back to the server name when no snapshot_id is given
    assert meta.labels[SNAPSHOT_ID_LABEL] == "srv"


async def test_snapshot_id_sets_group_label_without_ps_name(mocker):
    meta = await _deploy_pod_meta(mocker, snapshot_id="group-7")
    assert meta.labels[SNAPSHOT_ID_LABEL] == "group-7"
    assert PS_NAME_ANNOTATION not in meta.annotations


async def test_snapshot_tag_sets_ps_name_annotation(mocker):
    meta = await _deploy_pod_meta(mocker, snapshot_id="group-7", snapshot_tag="ps-abc")
    assert meta.annotations[PS_NAME_ANNOTATION] == "ps-abc"
    # the group label is still set so re-snapshots from the restored pod group correctly
    assert meta.labels[SNAPSHOT_ID_LABEL] == "group-7"
