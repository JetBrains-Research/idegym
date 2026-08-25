"""Unit tests for ``deploy_server`` pod-spec customization (volumes, mounts, env_from,
pod_overrides) and the related ``StartServerRequest`` fields.

The Kubernetes API is mocked so the test runs without a cluster. A real ``ApiClient`` is used
only for its (offline) camelCase deserializer.
"""

from types import SimpleNamespace

import pytest
from idegym.api.orchestrator.servers import StartServerRequest
from idegym.backend.utils import kubernetes_client as kc
from kubernetes_asyncio.client import ApiClient, V1Container, V1ObjectMeta, V1PodSpec, V1ResourceRequirements

SANDBOX_CAPACITY_OWNER = "grazie/idegym"


def _node(name, *, capacity=None, allocatable=None, owner=None, resource_version="1"):
    annotations = {kc.SANDBOX_CAPACITY_OWNER_ANNOTATION: owner} if owner else None
    return SimpleNamespace(
        metadata=V1ObjectMeta(
            name=name,
            annotations=annotations,
            resource_version=resource_version,
        ),
        status=SimpleNamespace(capacity=capacity or {}, allocatable=allocatable or {}),
    )


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
    return apps, core


async def _deploy_and_get_pod_spec(mocker, api_client, **kwargs):
    apps, _ = _patch_clients(mocker, api_client)
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


async def test_max_sandboxes_per_node_requests_scheduler_accounted_capacity(mocker, api_client):
    prepare_capacity = mocker.patch.object(kc, "prepare_sandbox_node_capacity", mocker.AsyncMock())
    apps, _ = _patch_clients(mocker, api_client)
    await kc.deploy_server(
        image_tag="img:latest",
        server_name="srv",
        namespace="ns",
        max_sandboxes_per_node=20,
        sandbox_capacity_owner=SANDBOX_CAPACITY_OWNER,
        resources={"requests": {"cpu": "1"}, "limits": {"memory": "2Gi"}},
    )
    deployment = apps.create_namespaced_deployment.await_args.kwargs["body"]
    template = deployment.spec.template
    resources = template.spec.containers[0].resources

    prepare_capacity.assert_awaited_once_with(20, SANDBOX_CAPACITY_OWNER)
    assert resources.requests == {"cpu": "1", kc.SANDBOX_CAPACITY_RESOURCE: "1"}
    assert resources.limits == {"memory": "2Gi", kc.SANDBOX_CAPACITY_RESOURCE: "1"}
    assert template.metadata.labels[kc.SANDBOX_COMPONENT_LABEL] == kc.SANDBOX_COMPONENT_VALUE
    assert deployment.spec.strategy.type == "Recreate"


async def test_max_sandboxes_per_node_rejects_negative_limit(mocker, api_client):
    with pytest.raises(ValueError, match="must be non-negative"):
        await _deploy_and_get_pod_spec(mocker, api_client, max_sandboxes_per_node=-1)


async def test_max_sandboxes_per_node_rejects_caller_managed_capacity(mocker, api_client):
    mocker.patch.object(kc, "prepare_sandbox_node_capacity", mocker.AsyncMock())
    with pytest.raises(ValueError, match="is managed"):
        await _deploy_and_get_pod_spec(
            mocker,
            api_client,
            max_sandboxes_per_node=20,
            sandbox_capacity_owner=SANDBOX_CAPACITY_OWNER,
            resources={"requests": {kc.SANDBOX_CAPACITY_RESOURCE: "2"}},
        )


@pytest.mark.parametrize("field", ["nodeName", "node_name", "schedulerName", "scheduler_name"])
async def test_max_sandboxes_per_node_rejects_scheduler_bypass(mocker, api_client, field):
    mocker.patch.object(kc, "prepare_sandbox_node_capacity", mocker.AsyncMock())
    with pytest.raises(ValueError, match="must not bypass scheduling"):
        await _deploy_and_get_pod_spec(
            mocker,
            api_client,
            max_sandboxes_per_node=20,
            sandbox_capacity_owner=SANDBOX_CAPACITY_OWNER,
            pod_overrides={field: "bypass"},
        )


async def test_prepare_sandbox_node_capacity_rejects_legacy_workloads(mocker):
    mocker.patch.object(kc, "_prepared_sandbox_capacity", None)
    reconcile = mocker.patch.object(kc, "reconcile_sandbox_node_capacity", mocker.AsyncMock())
    mocker.patch.object(
        kc,
        "find_legacy_sandbox_workloads",
        mocker.AsyncMock(return_value=["Deployment ns/legacy", "Pod ns/legacy-abc"]),
    )

    with pytest.raises(RuntimeError, match="drain them first"):
        await kc.prepare_sandbox_node_capacity(20, SANDBOX_CAPACITY_OWNER)

    reconcile.assert_not_awaited()


async def test_prepare_sandbox_node_capacity_validates_once_per_process(mocker):
    mocker.patch.object(kc, "_prepared_sandbox_capacity", None)
    find_legacy = mocker.patch.object(kc, "find_legacy_sandbox_workloads", mocker.AsyncMock(return_value=[]))
    reconcile = mocker.patch.object(kc, "reconcile_sandbox_node_capacity", mocker.AsyncMock())

    await kc.prepare_sandbox_node_capacity(20, SANDBOX_CAPACITY_OWNER)
    await kc.prepare_sandbox_node_capacity(20, SANDBOX_CAPACITY_OWNER)

    find_legacy.assert_awaited_once()
    reconcile.assert_awaited_once_with(20, SANDBOX_CAPACITY_OWNER)


async def test_find_legacy_sandbox_workloads_checks_templates_and_live_pods(mocker, api_client):
    apps, core = _patch_clients(mocker, api_client)
    legacy_spec = V1PodSpec(containers=[V1Container(name="server")])
    capped_spec = V1PodSpec(
        containers=[
            V1Container(
                name="server",
                resources=V1ResourceRequirements(requests={kc.SANDBOX_CAPACITY_RESOURCE: "1"}),
            )
        ]
    )
    zero_spec = V1PodSpec(
        containers=[
            V1Container(
                name="server",
                resources=V1ResourceRequirements(requests={kc.SANDBOX_CAPACITY_RESOURCE: "0"}),
            )
        ]
    )
    apps.list_deployment_for_all_namespaces = mocker.AsyncMock(
        return_value=SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=V1ObjectMeta(namespace="ns", name="legacy"),
                    spec=SimpleNamespace(template=SimpleNamespace(spec=legacy_spec)),
                ),
                SimpleNamespace(
                    metadata=V1ObjectMeta(namespace="ns", name="capped"),
                    spec=SimpleNamespace(template=SimpleNamespace(spec=capped_spec)),
                ),
                SimpleNamespace(
                    metadata=V1ObjectMeta(namespace="ns", name="zero-unit"),
                    spec=SimpleNamespace(template=SimpleNamespace(spec=zero_spec)),
                ),
            ]
        )
    )
    core.list_pod_for_all_namespaces = mocker.AsyncMock(
        return_value=SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=V1ObjectMeta(namespace="ns", name="legacy-pod"),
                    spec=legacy_spec,
                    status=SimpleNamespace(phase="Running"),
                ),
                SimpleNamespace(
                    metadata=V1ObjectMeta(namespace="ns", name="finished-pod"),
                    spec=legacy_spec,
                    status=SimpleNamespace(phase="Succeeded"),
                ),
            ]
        )
    )

    assert await kc.find_legacy_sandbox_workloads() == [
        "Deployment ns/legacy",
        "Deployment ns/zero-unit",
        "Pod ns/legacy-pod",
    ]


async def test_wait_for_sandbox_capacity_waits_for_allocatable(mocker):
    core = mocker.MagicMock()
    core.list_node = mocker.AsyncMock(
        side_effect=[
            SimpleNamespace(
                items=[
                    _node(
                        "node-a",
                        capacity={kc.SANDBOX_CAPACITY_RESOURCE: "20"},
                        allocatable={kc.SANDBOX_CAPACITY_RESOURCE: "10"},
                    )
                ]
            ),
            SimpleNamespace(
                items=[
                    _node(
                        "node-a",
                        capacity={kc.SANDBOX_CAPACITY_RESOURCE: "20"},
                        allocatable={kc.SANDBOX_CAPACITY_RESOURCE: "20"},
                    )
                ]
            ),
        ]
    )
    sleep = mocker.patch.object(kc, "sleep", mocker.AsyncMock())

    await kc._wait_for_sandbox_capacity(core, {"node-a"}, 20, timeout_seconds=1)

    sleep.assert_awaited_once()
    assert core.list_node.await_count == 2


async def test_wait_for_sandbox_capacity_has_bounded_timeout(mocker):
    core = mocker.MagicMock()
    core.list_node = mocker.AsyncMock(return_value=SimpleNamespace(items=[_node("node-a")]))

    with pytest.raises(RuntimeError, match="Timed out"):
        await kc._wait_for_sandbox_capacity(core, {"node-a"}, 20, timeout_seconds=0)


async def test_wait_for_sandbox_capacity_ignores_deleted_nodes(mocker):
    core = mocker.MagicMock()
    core.list_node = mocker.AsyncMock(return_value=SimpleNamespace(items=[]))
    sleep = mocker.patch.object(kc, "sleep", mocker.AsyncMock())

    await kc._wait_for_sandbox_capacity(core, {"deleted-node"}, 20, timeout_seconds=1)

    sleep.assert_not_awaited()


async def test_reconcile_rejects_conflicting_capacity_owner(mocker, api_client):
    _, core = _patch_clients(mocker, api_client)
    core.list_node = mocker.AsyncMock(return_value=SimpleNamespace(items=[_node("node-a", owner="other/idegym")]))

    with pytest.raises(RuntimeError, match="another IdeGYM installation"):
        await kc.reconcile_sandbox_node_capacity(20, SANDBOX_CAPACITY_OWNER)


async def test_claim_sandbox_capacity_owner_uses_atomic_anchor_patch(mocker):
    core = mocker.MagicMock()
    core.patch_node = mocker.AsyncMock()
    nodes = [_node("node-b", resource_version="12"), _node("node-a", resource_version="11")]

    await kc._claim_sandbox_capacity_owner(core, nodes, SANDBOX_CAPACITY_OWNER, 20)

    call = core.patch_node.await_args.kwargs
    assert call["name"] == "node-a"
    assert call["body"][0] == {
        "op": "test",
        "path": "/metadata/resourceVersion",
        "value": "11",
    }
    assert call["body"][1]["value"][kc.SANDBOX_CAPACITY_OWNER_ANNOTATION] == SANDBOX_CAPACITY_OWNER


@pytest.mark.parametrize("status", [404, 422])
async def test_claim_sandbox_capacity_owner_retries_stale_anchor(mocker, status):
    core = mocker.MagicMock()
    core.patch_node = mocker.AsyncMock(side_effect=[kc.ApiException(status=status), None])
    first = _node("node-a", resource_version="11")
    second = _node("node-a", resource_version="12")
    core.list_node = mocker.AsyncMock(return_value=SimpleNamespace(items=[second]))

    await kc._claim_sandbox_capacity_owner(core, [first], SANDBOX_CAPACITY_OWNER, 20)

    assert core.patch_node.await_count == 2
    assert core.patch_node.await_args.kwargs["body"][0]["value"] == "12"


async def test_cleanup_sandbox_capacity_refuses_requesting_pods(mocker, api_client):
    apps, core = _patch_clients(mocker, api_client)
    apps.list_deployment_for_all_namespaces = mocker.AsyncMock(return_value=SimpleNamespace(items=[]))
    requesting_spec = V1PodSpec(
        containers=[
            V1Container(
                name="server",
                resources=V1ResourceRequirements(requests={kc.SANDBOX_CAPACITY_RESOURCE: "1"}),
            )
        ]
    )
    core.list_pod_for_all_namespaces = mocker.AsyncMock(
        return_value=SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=V1ObjectMeta(namespace="ns", name="sandbox"),
                    spec=requesting_spec,
                    status=SimpleNamespace(phase="Running"),
                )
            ]
        )
    )
    owned = _node(
        "node-a",
        capacity={kc.SANDBOX_CAPACITY_RESOURCE: "20"},
        allocatable={kc.SANDBOX_CAPACITY_RESOURCE: "20"},
        owner=SANDBOX_CAPACITY_OWNER,
    )
    zeroed = _node(
        "node-a",
        capacity={kc.SANDBOX_CAPACITY_RESOURCE: "0"},
        allocatable={kc.SANDBOX_CAPACITY_RESOURCE: "0"},
        owner=SANDBOX_CAPACITY_OWNER,
    )
    owned.metadata.annotations[kc.SANDBOX_CAPACITY_LIMIT_ANNOTATION] = "20"
    core.list_node = mocker.AsyncMock(side_effect=[SimpleNamespace(items=[owned]), SimpleNamespace(items=[zeroed])])
    core.patch_node_status = mocker.AsyncMock()
    core.patch_node = mocker.AsyncMock()

    with pytest.raises(RuntimeError, match="requesting workloads exist"):
        await kc.cleanup_sandbox_node_capacity(SANDBOX_CAPACITY_OWNER)

    core.patch_node_status.assert_awaited_once()
    core.patch_node.assert_not_awaited()


async def test_cleanup_sandbox_capacity_zeros_nodes_and_releases_owner(mocker, api_client):
    apps, core = _patch_clients(mocker, api_client)
    apps.list_deployment_for_all_namespaces = mocker.AsyncMock(return_value=SimpleNamespace(items=[]))
    core.list_pod_for_all_namespaces = mocker.AsyncMock(return_value=SimpleNamespace(items=[]))
    owned = _node(
        "node-a",
        capacity={kc.SANDBOX_CAPACITY_RESOURCE: "20"},
        allocatable={kc.SANDBOX_CAPACITY_RESOURCE: "20"},
        owner=SANDBOX_CAPACITY_OWNER,
    )
    zeroed = _node(
        "node-a",
        capacity={kc.SANDBOX_CAPACITY_RESOURCE: "0"},
        allocatable={kc.SANDBOX_CAPACITY_RESOURCE: "0"},
        owner=SANDBOX_CAPACITY_OWNER,
    )
    owned.metadata.annotations[kc.SANDBOX_CAPACITY_LIMIT_ANNOTATION] = "20"
    core.list_node = mocker.AsyncMock(side_effect=[SimpleNamespace(items=[owned]), SimpleNamespace(items=[zeroed])])
    core.patch_node_status = mocker.AsyncMock()
    core.patch_node = mocker.AsyncMock()

    await kc.cleanup_sandbox_node_capacity(SANDBOX_CAPACITY_OWNER)

    core.patch_node_status.assert_awaited_once()
    assert core.patch_node_status.await_args.kwargs["body"][0]["value"] == "0"
    core.patch_node.assert_awaited_once()
    patch = core.patch_node.await_args.kwargs["body"]
    assert patch[0]["op"] == "test"
    assert patch[0]["value"] == SANDBOX_CAPACITY_OWNER
    assert [operation["op"] for operation in patch[1:]] == ["remove", "remove"]


async def test_release_sandbox_capacity_owner_does_not_erase_successor(mocker):
    core = mocker.MagicMock()
    core.patch_node = mocker.AsyncMock(side_effect=kc.ApiException(status=422))
    original = _node("node-a", owner=SANDBOX_CAPACITY_OWNER)
    successor = _node("node-a", owner="other/idegym")
    core.list_node = mocker.AsyncMock(return_value=SimpleNamespace(items=[successor]))

    with pytest.raises(RuntimeError, match="now owned by another"):
        await kc._release_sandbox_capacity_owner(core, original, SANDBOX_CAPACITY_OWNER)

    core.patch_node.assert_awaited_once()


async def test_release_sandbox_capacity_owner_accepts_deleted_node(mocker):
    core = mocker.MagicMock()
    core.patch_node = mocker.AsyncMock(side_effect=kc.ApiException(status=404))

    await kc._release_sandbox_capacity_owner(
        core,
        _node("deleted-node", owner=SANDBOX_CAPACITY_OWNER),
        SANDBOX_CAPACITY_OWNER,
    )

    core.list_node.assert_not_called()


async def test_reconcile_sandbox_node_capacity_patches_only_stale_nodes(mocker, api_client):
    _, core = _patch_clients(mocker, api_client)
    wait_for_capacity = mocker.patch.object(kc, "_wait_for_sandbox_capacity", mocker.AsyncMock())
    core.list_node = mocker.AsyncMock(
        return_value=SimpleNamespace(
            items=[
                SimpleNamespace(
                    metadata=V1ObjectMeta(name="fresh"),
                    status=SimpleNamespace(capacity={kc.SANDBOX_CAPACITY_RESOURCE: "20"}),
                ),
                SimpleNamespace(
                    metadata=V1ObjectMeta(name="stale"),
                    status=SimpleNamespace(capacity={}),
                ),
            ]
        )
    )
    core.patch_node_status = mocker.AsyncMock()
    core.patch_node = mocker.AsyncMock()

    await kc.reconcile_sandbox_node_capacity(20, SANDBOX_CAPACITY_OWNER)

    core.patch_node_status.assert_awaited_once_with(
        name="stale",
        body=[
            {
                "op": "add",
                "path": "/status/capacity/idegym.jetbrains.com~1sandbox",
                "value": "20",
            }
        ],
        _content_type="application/json-patch+json",
    )
    wait_for_capacity.assert_awaited_once_with(core, {"fresh", "stale"}, 20)


async def test_reconcile_updates_positive_cap_for_same_owner(mocker, api_client):
    _, core = _patch_clients(mocker, api_client)
    node = _node(
        "node-a",
        capacity={kc.SANDBOX_CAPACITY_RESOURCE: "20"},
        allocatable={kc.SANDBOX_CAPACITY_RESOURCE: "20"},
        owner=SANDBOX_CAPACITY_OWNER,
    )
    core.list_node = mocker.AsyncMock(return_value=SimpleNamespace(items=[node]))
    core.patch_node = mocker.AsyncMock()
    core.patch_node_status = mocker.AsyncMock()
    mocker.patch.object(kc, "_wait_for_sandbox_capacity", mocker.AsyncMock())

    await kc.reconcile_sandbox_node_capacity(10, SANDBOX_CAPACITY_OWNER)

    assert core.patch_node_status.await_args.kwargs["body"][0]["value"] == "10"
    annotations = core.patch_node.await_args.kwargs["body"]["metadata"]["annotations"]
    assert annotations[kc.SANDBOX_CAPACITY_LIMIT_ANNOTATION] == "10"


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


# ---------------------------------------------------------------------------
# Kaniko build: secret build-arg forwarding (private IDE plugin downloads)
# ---------------------------------------------------------------------------


def _patch_kaniko_clients(mocker):
    """Patch create_clients and return the batch mock capturing the submitted Job body."""
    job_result = mocker.MagicMock()
    job_result.api_version = "batch/v1"
    job_result.kind = "Job"
    job_result.metadata.name = "kaniko-build"
    job_result.metadata.uid = "uid-1"

    batch = mocker.MagicMock()
    batch.create_namespaced_job = mocker.AsyncMock(return_value=job_result)
    core = mocker.MagicMock()
    core.create_namespaced_config_map = mocker.AsyncMock()
    policy = mocker.MagicMock()
    policy.create_namespaced_pod_disruption_budget = mocker.AsyncMock()

    clients = (mocker.MagicMock(), batch, core, policy, mocker.MagicMock())
    mocker.patch.object(kc, "create_clients", mocker.AsyncMock(return_value=clients))
    return batch


async def _kaniko_job_body(mocker, **kwargs):
    batch = _patch_kaniko_clients(mocker)
    await kc.build_and_push_image_with_kaniko(
        tag="reg.example/idegym/img:v1",
        service_version="1.2.3",
        dockerfile_content="FROM debian\n",
        namespace="ns",
        insecure_registry=True,
        **kwargs,
    )
    return batch.create_namespaced_job.call_args.kwargs["body"]


async def _kaniko_job_container(mocker, **kwargs):
    body = await _kaniko_job_body(mocker, **kwargs)
    return body.spec.template.spec.containers[0]


async def test_kaniko_forwards_secret_build_arg_from_env(mocker):
    mocker.patch.dict("os.environ", {"PLUGIN_TOKEN": "s3cr3t"}, clear=False)
    container = await _kaniko_job_container(mocker, secret_build_args=["PLUGIN_TOKEN"])
    assert "--build-arg=PLUGIN_TOKEN=s3cr3t" in container.args
    # kaniko substitutes ARGs from --build-arg; the token must NOT be duplicated into env,
    # to avoid a second plaintext copy in the Job/Pod spec.
    assert not any(e.name == "PLUGIN_TOKEN" for e in container.env)


async def test_kaniko_omits_secret_build_arg_when_env_absent(mocker):
    mocker.patch.dict("os.environ", {}, clear=True)
    container = await _kaniko_job_container(mocker, secret_build_args=["PLUGIN_TOKEN"])
    assert not any(arg.startswith("--build-arg=PLUGIN_TOKEN=") for arg in container.args)
    assert not any(e.name == "PLUGIN_TOKEN" for e in container.env)


async def test_kaniko_omits_secret_build_arg_when_env_empty(mocker):
    mocker.patch.dict("os.environ", {"PLUGIN_TOKEN": ""}, clear=True)
    container = await _kaniko_job_container(mocker, secret_build_args=["PLUGIN_TOKEN"])
    assert not any(arg.startswith("--build-arg=PLUGIN_TOKEN=") for arg in container.args)


async def test_kaniko_secret_forwarding_does_not_clobber_job_name(mocker):
    # Regression: the secret-forwarding loop must not rebind the `name` used for the
    # Job/ConfigMap/PDB metadata (which must stay a valid RFC-1123 kaniko-build-<rand> name).
    mocker.patch.dict("os.environ", {"PLUGIN_TOKEN": "s3cr3t"}, clear=False)
    body = await _kaniko_job_body(mocker, secret_build_args=["PLUGIN_TOKEN"])
    assert body.metadata.name.startswith("kaniko-build-")
