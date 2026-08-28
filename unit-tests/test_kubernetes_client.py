"""Unit tests for ``deploy_server`` pod-spec customization (volumes, mounts, env_from,
pod_overrides) and the related ``StartServerRequest`` fields.

The Kubernetes API is mocked so the test runs without a cluster. A real ``ApiClient`` is used
only for its (offline) camelCase deserializer.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import structlog
from idegym.api.config import SchedulingConfig
from idegym.api.orchestrator.servers import StartServerRequest
from idegym.api.type import Duration
from idegym.backend.utils import kubernetes_client as kc
from kubernetes_asyncio.client import ApiClient, ApiException
from structlog.testing import capture_logs


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


# ---------------------------------------------------------------------------
# wait_for_pods_ready: unschedulable tolerance
# ---------------------------------------------------------------------------

# (pods_ready, has_image_pull_error, has_terminating_pods, has_unschedulable_pods)
_READY = (True, False, False, False)
_UNSCHEDULABLE = (False, False, False, True)
_PENDING = (False, False, False, False)
_IMAGE_PULL_ERROR = (False, True, False, False)


def _fast_scheduling(**overrides) -> SchedulingConfig:
    """Scheduling config whose polls are effectively instantaneous, so the tests stay fast."""
    return SchedulingConfig(poll_interval=Duration(milliseconds=1), **overrides)


def _patch_pods_are_ready(mocker, *results):
    return mocker.patch.object(kc, "pods_are_ready", mocker.AsyncMock(side_effect=list(results)))


def _patch_pods_stuck(mocker, result):
    """Patch the poll to keep returning ``result``, for the waits that are meant to give up."""
    return mocker.patch.object(kc, "pods_are_ready", mocker.AsyncMock(return_value=result))


def _patch_verdict(mocker, verdict=kc.NodeScalingVerdict.UNKNOWN, detail=None):
    """Patch the autoscaler lookup the wait consults for every unschedulable poll."""
    return mocker.patch.object(kc, "node_scaling_verdict", mocker.AsyncMock(return_value=(verdict, detail)))


def _patch_logger(mocker):
    """Rebind the module logger, so ``capture_logs`` can see what the wait emits.

    ``configure_logging`` configures structlog with ``cache_logger_on_first_use``, so a
    module-level logger that has already emitted under it is stuck with a bound logger for the
    rest of the process. ``capture_logs`` only swaps the global processor chain, so it captures
    nothing from that logger — and whether it is stuck depends on whether a test that calls
    ``configure_logging`` happened to run first, which is up to ``pytest-randomly``.
    """
    return mocker.patch.object(kc, "logger", structlog.get_logger(kc.__name__))


async def test_wait_returns_once_pods_are_ready(mocker):
    _patch_pods_are_ready(mocker, _PENDING, _READY)
    await kc.wait_for_pods_ready(label_selector="app=srv", namespace="ns", scheduling=_fast_scheduling())


async def test_wait_tolerates_many_unschedulable_polls_within_the_budget(mocker):
    # Regression: the limit used to be 15 consecutive polls, so a node pool that takes minutes to
    # scale up failed the wait long before its budget — however often we happened to poll.
    poll = _patch_pods_are_ready(mocker, *([_UNSCHEDULABLE] * 40), _READY)
    verdict = _patch_verdict(mocker)
    await kc.wait_for_pods_ready(
        label_selector="app=srv",
        namespace="ns",
        scheduling=_fast_scheduling(unschedulable_timeout=Duration(minutes=5)),
    )
    assert poll.await_count == 41
    # The autoscaler cannot shorten the budget, so within it there is nothing to ask about.
    verdict.assert_not_awaited()


async def test_wait_fails_once_the_unschedulable_budget_is_spent(mocker):
    _patch_pods_stuck(mocker, _UNSCHEDULABLE)
    _patch_verdict(mocker)
    with pytest.raises(RuntimeError, match="unschedulable for"):
        await kc.wait_for_pods_ready(
            label_selector="app=srv",
            namespace="ns",
            scheduling=_fast_scheduling(unschedulable_timeout=Duration(milliseconds=20)),
        )


async def test_zero_unschedulable_timeout_waits_for_the_overall_timeout(mocker):
    _patch_pods_stuck(mocker, _UNSCHEDULABLE)
    _patch_verdict(mocker)
    with pytest.raises(TimeoutError):
        await kc.wait_for_pods_ready(
            label_selector="app=srv",
            namespace="ns",
            wait_timeout=1,
            scheduling=_fast_scheduling(unschedulable_timeout=Duration(0)),
        )


async def test_becoming_schedulable_again_restarts_the_budget(mocker):
    # A pod that lands on a node and is later evicted back to Pending must get a fresh budget,
    # not inherit the time the previous scheduling attempt burned.
    _patch_pods_are_ready(
        mocker,
        _UNSCHEDULABLE,
        _UNSCHEDULABLE,
        _PENDING,
        *([_UNSCHEDULABLE] * 2),
        _READY,
    )
    _patch_verdict(mocker)
    await kc.wait_for_pods_ready(
        label_selector="app=srv",
        namespace="ns",
        scheduling=SchedulingConfig(poll_interval=Duration(milliseconds=20), unschedulable_timeout=Duration(seconds=1)),
    )


async def test_image_pull_errors_still_fail_fast(mocker):
    _patch_pods_stuck(mocker, _IMAGE_PULL_ERROR)
    with pytest.raises(RuntimeError, match="Image pull errors"):
        await kc.wait_for_pods_ready(
            label_selector="app=srv",
            namespace="ns",
            max_image_pull_attempts=3,
            scheduling=_fast_scheduling(),
        )


async def test_poll_interval_is_taken_from_the_scheduling_config(mocker):
    sleep = mocker.patch.object(kc, "sleep", mocker.AsyncMock())
    _patch_pods_are_ready(mocker, _PENDING, _READY)
    await kc.wait_for_pods_ready(
        label_selector="app=srv",
        namespace="ns",
        scheduling=SchedulingConfig(poll_interval=Duration(seconds=7)),
    )
    sleep.assert_awaited_once_with(7.0)


# ---------------------------------------------------------------------------
# wait_for_pods_ready: the autoscaler's verdict picks the budget
# ---------------------------------------------------------------------------


async def test_a_scale_up_in_flight_earns_the_longer_budget(mocker):
    # A booting node keeps the pod unschedulable well past the no-verdict budget; the wait must
    # spend the provisioning budget on it instead of failing.
    _patch_pods_are_ready(mocker, *([_UNSCHEDULABLE] * 40), _READY)
    _patch_verdict(mocker, kc.NodeScalingVerdict.IN_FLIGHT, "TriggeredScaleUp: pod triggered scale-up")
    await kc.wait_for_pods_ready(
        label_selector="app=srv",
        namespace="ns",
        scheduling=_fast_scheduling(
            unschedulable_timeout=Duration(milliseconds=1), provisioning_timeout=Duration(minutes=15)
        ),
    )


async def test_a_granted_provisioning_budget_survives_a_later_refusal(mocker):
    # The autoscaler flipping to "will not grow the pool" mid-boot must not retroactively shrink the
    # budget the in-flight scale-up already earned, or a wait 6 minutes into a legitimate 15-minute
    # node boot dies the moment the verdict changes.
    _patch_pods_are_ready(mocker, *([_UNSCHEDULABLE] * 40), _READY)
    mocker.patch.object(
        kc,
        "node_scaling_verdict",
        mocker.AsyncMock(
            side_effect=[
                (kc.NodeScalingVerdict.IN_FLIGHT, "TriggeredScaleUp: pod triggered scale-up"),
                *[(kc.NodeScalingVerdict.REFUSED, "NotTriggerScaleUp: max node group size reached")] * 40,
            ]
        ),
    )
    await kc.wait_for_pods_ready(
        label_selector="app=srv",
        namespace="ns",
        scheduling=_fast_scheduling(
            unschedulable_timeout=Duration(milliseconds=1), provisioning_timeout=Duration(minutes=15)
        ),
    )


async def test_a_disabled_budget_is_not_overridden_by_a_finite_one(mocker):
    # unschedulable_timeout=0 means "no limit"; an IN_FLIGHT verdict offering a finite provisioning
    # budget must not turn that back into a limit.
    _patch_pods_stuck(mocker, _UNSCHEDULABLE)
    verdict = _patch_verdict(mocker, kc.NodeScalingVerdict.IN_FLIGHT, "TriggeredScaleUp: growing")
    with pytest.raises(TimeoutError):
        await kc.wait_for_pods_ready(
            label_selector="app=srv",
            namespace="ns",
            wait_timeout=1,
            scheduling=_fast_scheduling(
                unschedulable_timeout=Duration(0), provisioning_timeout=Duration(milliseconds=10)
            ),
        )
    verdict.assert_not_awaited()


async def test_a_refused_scale_up_keeps_the_short_budget_and_names_the_reason(mocker):
    # A refusal is not a verdict on capacity a finishing pod is about to free, so it must not cut
    # the wait short — it only earns the pod the shorter budget and a better error.
    _patch_pods_stuck(mocker, _UNSCHEDULABLE)
    _patch_verdict(mocker, kc.NodeScalingVerdict.REFUSED, "NotTriggerScaleUp: max node group size reached")
    with pytest.raises(RuntimeError, match="scale-up refused.*max node group size reached"):
        await kc.wait_for_pods_ready(
            label_selector="app=srv",
            namespace="ns",
            scheduling=_fast_scheduling(
                unschedulable_timeout=Duration(milliseconds=20), provisioning_timeout=Duration(minutes=15)
            ),
        )


# ---------------------------------------------------------------------------
# node_scaling_verdict
# ---------------------------------------------------------------------------


def _event(reason: str, message: str = "", *, at: int = 0):
    return SimpleNamespace(
        reason=reason, message=message, last_timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=at)
    )


def _patch_events(mocker, pods, events):
    core = mocker.MagicMock()
    core.list_namespaced_pod = mocker.AsyncMock(return_value=SimpleNamespace(items=pods))
    core.list_namespaced_event = mocker.AsyncMock(return_value=SimpleNamespace(items=events))
    clients = (mocker.MagicMock(), mocker.MagicMock(), core, mocker.MagicMock(), mocker.MagicMock())
    mocker.patch.object(kc, "create_clients", mocker.AsyncMock(return_value=clients))
    return core


def _pending_pod(name: str = "srv-1"):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, deletion_timestamp=None), spec=SimpleNamespace(node_name=None)
    )


async def test_verdict_is_in_flight_when_the_autoscaler_triggered_a_scale_up(mocker):
    _patch_events(mocker, [_pending_pod()], [_event("TriggeredScaleUp", "pod triggered scale-up: [...]")])
    verdict, detail = await kc.node_scaling_verdict("app=srv", "ns")
    assert verdict is kc.NodeScalingVerdict.IN_FLIGHT
    assert "pod triggered scale-up" in detail


async def test_verdict_is_refused_when_the_autoscaler_declined(mocker):
    _patch_events(mocker, [_pending_pod()], [_event("NotTriggerScaleUp", "max node group size reached")])
    verdict, detail = await kc.node_scaling_verdict("app=srv", "ns")
    assert verdict is kc.NodeScalingVerdict.REFUSED
    assert "max node group size reached" in detail


async def test_the_newest_autoscaler_event_decides(mocker):
    # A refusal that a later scale-up superseded must not keep failing the wait.
    _patch_events(
        mocker,
        [_pending_pod()],
        [_event("NotTriggerScaleUp", "no matching node group", at=0), _event("TriggeredScaleUp", "growing", at=5)],
    )
    verdict, _ = await kc.node_scaling_verdict("app=srv", "ns")
    assert verdict is kc.NodeScalingVerdict.IN_FLIGHT


async def test_failed_scheduling_alone_is_no_verdict(mocker):
    # Every unscheduled pod gets a FailedScheduling event, including ones a node is booting for.
    _patch_events(mocker, [_pending_pod()], [_event("FailedScheduling", "0/3 nodes are available")])
    assert await kc.node_scaling_verdict("app=srv", "ns") == (kc.NodeScalingVerdict.UNKNOWN, None)


async def test_pods_already_placed_on_a_node_are_not_asked_about(mocker):
    placed = SimpleNamespace(
        metadata=SimpleNamespace(name="srv-1", deletion_timestamp=None), spec=SimpleNamespace(node_name="node-a")
    )
    core = _patch_events(mocker, [placed], [_event("TriggeredScaleUp", "growing")])
    assert await kc.node_scaling_verdict("app=srv", "ns") == (kc.NodeScalingVerdict.UNKNOWN, None)
    core.list_namespaced_event.assert_not_awaited()


async def test_a_failure_listing_pods_is_not_reported_as_unreadable_events(mocker):
    # Reporting it as UNREADABLE would point an operator at the brand-new events RBAC rule.
    core = _patch_events(mocker, [_pending_pod()], [])
    core.list_namespaced_pod = mocker.AsyncMock(side_effect=ApiException(status=500, reason="Internal Server Error"))
    with pytest.raises(ApiException):
        await kc.node_scaling_verdict("app=srv", "ns")


async def test_unreadable_events_are_their_own_verdict(mocker):
    # A deployment whose RBAC predates the events read must keep working, without the distinction.
    core = _patch_events(mocker, [_pending_pod()], [])
    core.list_namespaced_event = mocker.AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))
    assert await kc.node_scaling_verdict("app=srv", "ns") == (kc.NodeScalingVerdict.UNREADABLE, "Forbidden")


async def test_unreadable_events_keep_the_short_budget_and_are_warned_about_once(mocker):
    # The lookup runs on every poll, so the missing-permission warning must not become spam — and an
    # unreadable verdict must not earn the pods the longer provisioning budget.
    poll = _patch_pods_stuck(mocker, _UNSCHEDULABLE)
    verdict = _patch_verdict(mocker, kc.NodeScalingVerdict.UNREADABLE, "Forbidden")
    _patch_logger(mocker)
    with capture_logs() as logs, pytest.raises(RuntimeError, match="unreadable"):
        await kc.wait_for_pods_ready(
            label_selector="app=srv",
            namespace="ns",
            scheduling=_fast_scheduling(
                unschedulable_timeout=Duration(milliseconds=20), provisioning_timeout=Duration(minutes=15)
            ),
        )
    assert poll.await_count > 1, "the wait must have polled repeatedly before spending its budget"
    # One lookup, at the moment the budget ran out — so one warning, with its context bound.
    verdict.assert_awaited_once()
    warnings = [entry for entry in logs if entry["log_level"] == "warning" and "scheduling events" in entry["event"]]
    assert len(warnings) == 1
    assert warnings[0]["namespace"] == "ns"
    assert warnings[0]["reason"] == "Forbidden"
