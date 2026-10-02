"""Unit tests for deploy_server snapshot label/annotation wiring.

All Kubernetes clients are mocked so the deployment body can be inspected without a cluster.
"""

import pytest
from idegym.backend.utils.kubernetes_client import deploy_server

pytestmark = pytest.mark.unit

PS_NAME_ANNOTATION = "podsnapshot.gke.io/ps-name"
SNAPSHOT_ID_LABEL = "idegym.jetbrains.com/snapshot-id"


async def _deploy_pod_meta(kube_clients, **kwargs):
    await deploy_server(image_tag="img:latest", server_name="srv", namespace="idegym", **kwargs)
    body = kube_clients.apps.create_namespaced_deployment.await_args.kwargs["body"]
    return body.spec.template.metadata


async def test_no_snapshot_tag_omits_ps_name_annotation(kube_clients):
    meta = await _deploy_pod_meta(kube_clients)
    assert PS_NAME_ANNOTATION not in meta.annotations
    # snapshot-id label falls back to the server name when no snapshot_id is given
    assert meta.labels[SNAPSHOT_ID_LABEL] == "srv"


async def test_snapshot_id_sets_group_label_without_ps_name(kube_clients):
    meta = await _deploy_pod_meta(kube_clients, snapshot_id="group-7")
    assert meta.labels[SNAPSHOT_ID_LABEL] == "group-7"
    assert PS_NAME_ANNOTATION not in meta.annotations


async def test_snapshot_tag_sets_ps_name_annotation(kube_clients):
    meta = await _deploy_pod_meta(kube_clients, snapshot_id="group-7", snapshot_tag="ps-abc")
    assert meta.annotations[PS_NAME_ANNOTATION] == "ps-abc"
    # the group label is still set so re-snapshots from the restored pod group correctly
    assert meta.labels[SNAPSHOT_ID_LABEL] == "group-7"
