"""Unit tests for ``deploy_server`` pod-spec customization (volumes, mounts, env_from,
pod_overrides) and the related ``StartServerRequest`` fields.

The Kubernetes API is mocked so the test runs without a cluster. A real ``ApiClient`` is used
only for its (offline) camelCase deserializer.
"""

import pytest
from idegym.api.orchestrator.servers import StartServerRequest
from idegym.backend.utils import kubernetes_client as kc
from kubernetes_asyncio.client import ApiClient


@pytest.fixture
async def api_client():
    client = ApiClient()
    try:
        yield client
    finally:
        await client.close()


def _patch_clients(mocker, api_client):
    """Patch create_clients (used directly and by async_kube_api) and capture deployment bodies."""
    deployment_result = mocker.MagicMock()
    deployment_result.api_version = "apps/v1"
    deployment_result.kind = "Deployment"
    deployment_result.metadata.name = "srv"
    deployment_result.metadata.uid = "uid-123"

    apps = mocker.MagicMock()
    apps.api_client = api_client
    apps.create_namespaced_deployment = mocker.AsyncMock(return_value=deployment_result)

    core = mocker.MagicMock()
    core.create_namespaced_service = mocker.AsyncMock()
    policy = mocker.MagicMock()
    policy.create_namespaced_pod_disruption_budget = mocker.AsyncMock()

    clients = (apps, mocker.MagicMock(), core, policy, mocker.MagicMock())
    mocker.patch.object(kc, "create_clients", mocker.AsyncMock(return_value=clients))
    return apps


async def _deploy_and_get_pod_spec(mocker, api_client, **kwargs):
    apps = _patch_clients(mocker, api_client)
    await kc.deploy_server(image_tag="img:latest", server_name="srv", namespace="ns", **kwargs)
    body = apps.create_namespaced_deployment.call_args.kwargs["body"]
    return body.spec.template.spec


async def test_deploy_without_overrides_leaves_pod_unchanged(mocker, api_client):
    pod = await _deploy_and_get_pod_spec(mocker, api_client)
    container = pod.containers[0]
    assert container.name == "server"
    assert pod.volumes is None
    assert container.volume_mounts is None
    assert container.env_from is None


async def test_deploy_applies_typed_volumes_mounts_and_env_from(mocker, api_client):
    pod = await _deploy_and_get_pod_spec(
        mocker,
        api_client,
        volumes=[{"name": "creds", "secret": {"secretName": "agent-creds"}}],
        volume_mounts=[{"name": "creds", "mountPath": "/etc/creds", "readOnly": True}],
        env_from=[{"secretRef": {"name": "agent-creds"}}],
    )
    container = pod.containers[0]

    assert pod.volumes[0].name == "creds"
    assert pod.volumes[0].secret.secret_name == "agent-creds"  # camelCase mapped to snake_case
    assert container.volume_mounts[0].mount_path == "/etc/creds"
    assert container.volume_mounts[0].read_only is True
    assert container.env_from[0].secret_ref.name == "agent-creds"


async def test_pod_overrides_merge_pod_level_fields(mocker, api_client):
    pod = await _deploy_and_get_pod_spec(
        mocker,
        api_client,
        pod_overrides={
            "tolerations": [{"key": "dedicated", "operator": "Exists", "effect": "NoSchedule"}],
            "hostAliases": [{"ip": "10.0.0.1", "hostnames": ["agent.local"]}],
        },
    )
    # Managed container survives the sanitize -> merge -> deserialize roundtrip.
    assert [c.name for c in pod.containers] == ["server"]
    assert pod.tolerations[-1].key == "dedicated"
    assert pod.host_aliases[0].ip == "10.0.0.1"


async def test_pod_overrides_concatenate_lists_keeping_managed_entries(mocker, api_client):
    # node_pool_taint_key adds a managed toleration; an override toleration is appended, not replaced.
    pod = await _deploy_and_get_pod_spec(
        mocker,
        api_client,
        node_pool_taint_key="node-pool",
        pod_overrides={"tolerations": [{"key": "dedicated", "operator": "Exists", "effect": "NoSchedule"}]},
    )
    toleration_keys = {t.key for t in pod.tolerations}
    assert toleration_keys == {"node-pool", "dedicated"}


async def test_pod_overrides_can_add_sidecar_without_dropping_server(mocker, api_client):
    pod = await _deploy_and_get_pod_spec(
        mocker,
        api_client,
        pod_overrides={"containers": [{"name": "sidecar", "image": "busybox:latest"}]},
    )
    assert [c.name for c in pod.containers] == ["server", "sidecar"]


async def test_pod_overrides_rejects_service_account_name(mocker, api_client):
    with pytest.raises(ValueError, match="serviceAccountName"):
        await _deploy_and_get_pod_spec(
            mocker,
            api_client,
            service_account_name="managed-sa",
            pod_overrides={"serviceAccountName": "attacker-sa"},
        )


async def test_pod_overrides_rejects_replacing_managed_server_container(mocker, api_client):
    with pytest.raises(ValueError, match="server"):
        await _deploy_and_get_pod_spec(
            mocker,
            api_client,
            pod_overrides={"containers": [{"name": "server", "image": "evil:latest"}]},
        )


async def test_pod_overrides_rejects_non_list_containers(mocker, api_client):
    with pytest.raises(ValueError, match="must be a list"):
        await _deploy_and_get_pod_spec(
            mocker,
            api_client,
            pod_overrides={"containers": {"name": "server"}},
        )


async def test_pod_overrides_null_containers_keeps_managed_server(mocker, api_client):
    # {"containers": null} is dropped (not treated as a replacement), so the server survives.
    pod = await _deploy_and_get_pod_spec(
        mocker,
        api_client,
        pod_overrides={"containers": None},
    )
    assert [c.name for c in pod.containers] == ["server"]


async def test_pod_overrides_null_values_do_not_drop_managed_fields(mocker, api_client):
    # A null override must not delete a managed field (here: runtimeClassName stays "gvisor").
    pod = await _deploy_and_get_pod_spec(
        mocker,
        api_client,
        runtime_class_name="gvisor",
        pod_overrides={"runtimeClassName": None},
    )
    assert pod.runtime_class_name == "gvisor"
    assert [c.name for c in pod.containers] == ["server"]


def test_start_server_request_pod_fields_default_empty():
    request = StartServerRequest(client_id="00000000-0000-0000-0000-000000000000", image_tag="img:latest")
    assert request.volumes == []
    assert request.volume_mounts == []
    assert request.env_from == []
    assert request.service_account_name is None
    assert request.pod_overrides.model_dump(by_alias=True, exclude_none=True) == {}


def test_start_server_request_accepts_camelcase_pod_specs():
    request = StartServerRequest(
        client_id="00000000-0000-0000-0000-000000000000",
        image_tag="img:latest",
        volumes=[{"name": "creds", "secret": {"secretName": "agent-creds"}}],
        volume_mounts=[{"name": "creds", "mountPath": "/etc/creds", "readOnly": True}],
        env_from=[{"secretRef": {"name": "agent-creds"}}],
        service_account_name="agent-runner",
        pod_overrides={"tolerations": [{"key": "dedicated", "operator": "Exists"}]},
    )
    # camelCase input is parsed into typed models exposing snake_case attributes
    assert request.volumes[0].secret.secret_name == "agent-creds"
    assert request.volume_mounts[0].mount_path == "/etc/creds"
    assert request.env_from[0].secret_ref.name == "agent-creds"
    assert request.service_account_name == "agent-runner"
    assert request.pod_overrides.tolerations[0].key == "dedicated"
    # ...and dumps back to the native camelCase shape for the Kubernetes client
    assert request.volumes[0].model_dump(by_alias=True, exclude_none=True) == {
        "name": "creds",
        "secret": {"secretName": "agent-creds"},
    }
