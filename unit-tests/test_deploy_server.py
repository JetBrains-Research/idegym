"""Unit tests for deploy_server: one plain Pod per server, shared labels, snapshot wiring.

All Kubernetes clients are mocked so the pod body can be inspected without a cluster. A real
``ApiClient`` is used only for its (offline) camelCase serializer, which produces the manifest
``deploy_server`` returns.
"""

from types import SimpleNamespace

import pytest
from idegym.backend.utils.kubernetes_client import (
    SANDBOX_LABELS,
    SERVER_ANNOTATION,
    SNAPSHOT_ID_KEY,
    deploy_server,
)
from kubernetes_asyncio.client import ApiClient

pytestmark = pytest.mark.unit

PS_NAME_ANNOTATION = "podsnapshot.gke.io/ps-name"


@pytest.fixture
async def api_client():
    client = ApiClient()
    try:
        yield client
    finally:
        await client.close()


def _mock_clients(mocker, api_client):
    apps = mocker.MagicMock()
    apps.api_client = api_client
    core = mocker.MagicMock()
    policy = mocker.MagicMock()
    custom = mocker.MagicMock()
    batch = mocker.MagicMock()

    created_pod = SimpleNamespace(metadata=SimpleNamespace(name="srv", uid="uid-1"))
    core.create_namespaced_pod = mocker.AsyncMock(return_value=created_pod)
    apps.create_namespaced_deployment = mocker.AsyncMock()
    core.create_namespaced_service = mocker.AsyncMock()
    policy.create_namespaced_pod_disruption_budget = mocker.AsyncMock()

    mocker.patch(
        "idegym.backend.utils.kubernetes_client.create_clients",
        new=mocker.AsyncMock(return_value=(apps, batch, core, policy, custom)),
    )
    return SimpleNamespace(apps=apps, core=core, policy=policy)


async def _deploy(mocker, api_client, **kwargs):
    clients = _mock_clients(mocker, api_client)
    result = await deploy_server(image_tag="img:latest", server_name="srv", namespace="idegym", **kwargs)
    body = clients.core.create_namespaced_pod.await_args.kwargs["body"]
    return clients, body, result


async def test_creates_one_pod_and_nothing_else(mocker, api_client):
    clients, body, (created, manifest) = await _deploy(mocker, api_client)

    assert body.kind == "Pod"
    assert body.metadata.name == "srv"
    assert clients.core.create_namespaced_pod.await_args.kwargs["namespace"] == "idegym"
    clients.apps.create_namespaced_deployment.assert_not_called()
    clients.core.create_namespaced_service.assert_not_called()
    clients.policy.create_namespaced_pod_disruption_budget.assert_not_called()
    assert created.metadata.name == "srv"


async def test_pod_carries_shared_labels_and_server_annotation(mocker, api_client):
    _, body, _ = await _deploy(mocker, api_client)

    assert body.metadata.labels == SANDBOX_LABELS
    assert body.metadata.annotations[SERVER_ANNOTATION] == "srv"
    # snapshot-id falls back to the server name and is an annotation, not a label
    assert body.metadata.annotations[SNAPSHOT_ID_KEY] == "srv"
    assert SNAPSHOT_ID_KEY not in body.metadata.labels
    assert PS_NAME_ANNOTATION not in body.metadata.annotations


async def test_manifest_is_the_submitted_pod(mocker, api_client):
    _, body, (_, manifest) = await _deploy(mocker, api_client, container_port=9000)

    assert manifest["kind"] == "Pod"
    assert manifest["apiVersion"] == "v1"
    assert manifest["metadata"]["name"] == "srv"
    assert manifest["metadata"]["labels"] == SANDBOX_LABELS
    assert "resourceVersion" not in manifest["metadata"]
    assert manifest["spec"]["containers"][0]["ports"][0]["containerPort"] == 9000
    assert body.spec.containers[0].ports[0].container_port == 9000


async def test_snapshot_id_is_an_annotation_by_default(mocker, api_client):
    _, body, _ = await _deploy(mocker, api_client, snapshot_id="group-7")

    assert body.metadata.annotations[SNAPSHOT_ID_KEY] == "group-7"
    assert SNAPSHOT_ID_KEY not in body.metadata.labels
    assert PS_NAME_ANNOTATION not in body.metadata.annotations


async def test_snapshot_label_flag_adds_group_label(mocker, api_client):
    _, body, _ = await _deploy(mocker, api_client, snapshot_id="group-7", snapshot_label=True)

    assert body.metadata.labels[SNAPSHOT_ID_KEY] == "group-7"
    assert {k: v for k, v in body.metadata.labels.items() if k != SNAPSHOT_ID_KEY} == SANDBOX_LABELS


async def test_snapshot_tag_sets_ps_name_annotation(mocker, api_client):
    _, body, _ = await _deploy(mocker, api_client, snapshot_id="group-7", snapshot_tag="ps-abc", snapshot_label=True)

    assert body.metadata.annotations[PS_NAME_ANNOTATION] == "ps-abc"
    # the group label is still set so re-snapshots from the restored pod group correctly
    assert body.metadata.labels[SNAPSHOT_ID_KEY] == "group-7"
