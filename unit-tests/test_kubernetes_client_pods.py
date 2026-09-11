"""Unit tests for the pod-level helpers behind plain-Pod sandboxes.

``wait_for_pod_ready`` / ``is_server_pod_alive`` / ``clean_up_server`` / ``restart_server_pod``
run against a mocked CoreV1Api (``create_clients`` patched), with the module's ``sleep``
patched out so the polling loops finish instantly.
"""

from types import SimpleNamespace

import pytest
from idegym.api.exceptions import ResourceDeletionFailedException
from idegym.api.type import ConditionStatus
from idegym.backend.utils import kubernetes_client as kc
from kubernetes_asyncio.client import ApiException

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fabricated pods
# ---------------------------------------------------------------------------


def _container(*, ready=True, waiting_reason=None):
    waiting = SimpleNamespace(reason=waiting_reason, message="pull failed") if waiting_reason else None
    return SimpleNamespace(ready=ready, state=SimpleNamespace(waiting=waiting))


def _pod(
    name="srv-1",
    *,
    phase="Running",
    containers=None,
    conditions=None,
    deletion_timestamp=None,
    pod_ip="10.0.0.5",
    reason=None,
    message=None,
):
    if containers is None:
        containers = [_container()]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, deletion_timestamp=deletion_timestamp),
        status=SimpleNamespace(
            phase=phase,
            pod_ip=pod_ip,
            reason=reason,
            message=message,
            conditions=conditions or [],
            container_statuses=containers,
        ),
    )


def _unschedulable():
    return SimpleNamespace(
        type="PodScheduled", status=ConditionStatus.FALSE, reason="Unschedulable", message="0/3 nodes"
    )


def _patch_core(mocker):
    core = mocker.MagicMock()
    apps = mocker.MagicMock()
    clients = (apps, mocker.MagicMock(), core, mocker.MagicMock(), mocker.MagicMock())
    mocker.patch.object(kc, "create_clients", mocker.AsyncMock(return_value=clients))
    mocker.patch.object(kc, "sleep", mocker.AsyncMock())
    return core, apps


# ---------------------------------------------------------------------------
# wait_for_pod_ready
# ---------------------------------------------------------------------------


async def test_wait_for_pod_ready_polls_through_404_and_pending(mocker):
    core, _ = _patch_core(mocker)
    ready = _pod()
    core.read_namespaced_pod = mocker.AsyncMock(
        side_effect=[
            ApiException(status=404),
            _pod(phase="Pending", containers=[_container(ready=False)], pod_ip=None),
            ready,
        ]
    )

    pod = await kc.wait_for_pod_ready("srv-1", "ns", wait_timeout=10)

    assert pod is ready
    assert core.read_namespaced_pod.await_count == 3
    assert core.read_namespaced_pod.await_args.kwargs == {"name": "srv-1", "namespace": "ns"}


async def test_wait_for_pod_ready_requires_every_container_ready(mocker):
    core, _ = _patch_core(mocker)
    ready = _pod(containers=[_container(), _container()])
    core.read_namespaced_pod = mocker.AsyncMock(
        side_effect=[_pod(containers=[_container(), _container(ready=False)]), ready]
    )

    assert await kc.wait_for_pod_ready("srv-1", "ns", wait_timeout=10) is ready


async def test_wait_for_pod_ready_fails_fast_on_terminal_phase(mocker):
    core, _ = _patch_core(mocker)
    core.read_namespaced_pod = mocker.AsyncMock(return_value=_pod(phase="Failed", reason="Evicted", message="disk"))

    with pytest.raises(Exception, match="phase Failed"):
        await kc.wait_for_pod_ready("srv-1", "ns", wait_timeout=10)
    assert core.read_namespaced_pod.await_count == 1


async def test_wait_for_pod_ready_fails_fast_when_unschedulable(mocker):
    core, _ = _patch_core(mocker)
    pending = _pod(phase="Pending", containers=[], conditions=[_unschedulable()], pod_ip=None)
    core.read_namespaced_pod = mocker.AsyncMock(return_value=pending)

    with pytest.raises(Exception, match="Unschedulable"):
        await kc.wait_for_pod_ready("srv-1", "ns", wait_timeout=10)
    assert core.read_namespaced_pod.await_count == kc._MAX_CONSECUTIVE_UNSCHEDULABLE


async def test_wait_for_pod_ready_fails_fast_on_image_pull_errors(mocker):
    core, _ = _patch_core(mocker)
    pulling = _pod(phase="Pending", containers=[_container(ready=False, waiting_reason="ImagePullBackOff")])
    core.read_namespaced_pod = mocker.AsyncMock(return_value=pulling)

    with pytest.raises(Exception, match="Image pull errors"):
        await kc.wait_for_pod_ready("srv-1", "ns", wait_timeout=10, max_image_pull_attempts=3)
    assert core.read_namespaced_pod.await_count == 3


async def test_wait_for_pod_ready_reraises_non_404_api_errors(mocker):
    core, _ = _patch_core(mocker)
    core.read_namespaced_pod = mocker.AsyncMock(side_effect=ApiException(status=500))

    with pytest.raises(ApiException):
        await kc.wait_for_pod_ready("srv-1", "ns", wait_timeout=10)


# ---------------------------------------------------------------------------
# is_server_pod_alive
# ---------------------------------------------------------------------------


async def test_is_server_pod_alive_by_name(mocker):
    core, _ = _patch_core(mocker)
    core.list_namespaced_pod = mocker.AsyncMock(return_value=SimpleNamespace(items=[_pod()]))

    assert await kc.is_server_pod_alive("srv-1", "ns") is True
    assert core.list_namespaced_pod.await_args.kwargs == {"namespace": "ns", "field_selector": "metadata.name=srv-1"}


async def test_is_server_pod_alive_falls_back_to_legacy_label(mocker):
    core, _ = _patch_core(mocker)
    core.list_namespaced_pod = mocker.AsyncMock(
        side_effect=[SimpleNamespace(items=[]), SimpleNamespace(items=[_pod(name="srv-1-abc-xyz")])]
    )

    assert await kc.is_server_pod_alive("srv-1", "ns") is True
    assert core.list_namespaced_pod.await_args.kwargs == {"namespace": "ns", "label_selector": "app=srv-1"}


async def test_is_server_pod_alive_false_when_absent_or_terminating(mocker):
    core, _ = _patch_core(mocker)
    core.list_namespaced_pod = mocker.AsyncMock(side_effect=[SimpleNamespace(items=[]), SimpleNamespace(items=[])])
    assert await kc.is_server_pod_alive("srv-1", "ns") is False

    core.list_namespaced_pod = mocker.AsyncMock(
        return_value=SimpleNamespace(items=[_pod(deletion_timestamp="2026-06-12T00:00:00Z")])
    )
    assert await kc.is_server_pod_alive("srv-1", "ns") is False


# ---------------------------------------------------------------------------
# clean_up_server
# ---------------------------------------------------------------------------


async def test_clean_up_server_deletes_pod_then_legacy_deployment(mocker):
    core, apps = _patch_core(mocker)
    check_and_delete = mocker.patch.object(kc, "check_and_delete", mocker.AsyncMock(side_effect=[True, True]))

    await kc.clean_up_server("srv-1", "ns")

    assert [call.kwargs["resource_type"] for call in check_and_delete.await_args_list] == ["pod", "deployment"]
    pod_call, deployment_call = check_and_delete.await_args_list
    assert pod_call.kwargs["query_func"] is core.list_namespaced_pod
    assert pod_call.kwargs["delete_func"] is core.delete_namespaced_pod
    assert deployment_call.kwargs["query_func"] is apps.list_namespaced_deployment
    assert deployment_call.kwargs["delete_func"] is apps.delete_namespaced_deployment


async def test_clean_up_server_raises_when_pod_delete_fails(mocker):
    _patch_core(mocker)
    mocker.patch.object(kc, "check_and_delete", mocker.AsyncMock(side_effect=[False, True]))

    with pytest.raises(ResourceDeletionFailedException, match="pod"):
        await kc.clean_up_server("srv-1", "ns")


async def test_clean_up_server_tolerates_legacy_deployment_failure(mocker):
    _patch_core(mocker)
    mocker.patch.object(kc, "check_and_delete", mocker.AsyncMock(side_effect=[True, RuntimeError("boom")]))

    await kc.clean_up_server("srv-1", "ns")


# ---------------------------------------------------------------------------
# restart_server_pod
# ---------------------------------------------------------------------------


async def test_restart_server_pod_replays_manifest(mocker):
    core, _ = _patch_core(mocker)
    manifest = {"kind": "Pod", "metadata": {"name": "srv-1"}}
    new_pod = _pod(pod_ip="10.0.0.9")
    delete = mocker.patch.object(kc, "delete_with_retries", mocker.AsyncMock(return_value=True))
    core.read_namespaced_pod = mocker.AsyncMock(side_effect=[_pod(), ApiException(status=404)])
    core.create_namespaced_pod = mocker.AsyncMock()
    wait_ready = mocker.patch.object(kc, "wait_for_pod_ready", mocker.AsyncMock(return_value=new_pod))

    result = await kc.restart_server_pod("srv-1", "ns", manifest=manifest, wait_timeout=30)

    assert result is new_pod
    delete.assert_awaited_once_with(core.delete_namespaced_pod, "pod", "srv-1", "ns", 3)
    # the create happens only after the old pod name is gone
    assert core.read_namespaced_pod.await_count == 2
    assert core.create_namespaced_pod.await_args.kwargs == {"body": manifest, "namespace": "ns"}
    wait_ready.assert_awaited_once_with(pod_name="srv-1", namespace="ns", wait_timeout=30)


async def test_restart_server_pod_without_manifest_uses_legacy_path(mocker):
    _patch_core(mocker)
    legacy = mocker.patch.object(kc, "_restart_legacy_pods", mocker.AsyncMock())
    create = mocker.patch.object(kc, "wait_for_pod_ready", mocker.AsyncMock())

    assert await kc.restart_server_pod("srv-1", "ns", manifest=None, wait_timeout=30) is None
    legacy.assert_awaited_once_with("srv-1", "ns", wait_timeout=30, max_retries=3)
    create.assert_not_awaited()


async def test_restart_server_pod_raises_when_delete_fails(mocker):
    core, _ = _patch_core(mocker)
    mocker.patch.object(kc, "delete_with_retries", mocker.AsyncMock(return_value=False))
    core.create_namespaced_pod = mocker.AsyncMock()

    with pytest.raises(ResourceDeletionFailedException):
        await kc.restart_server_pod("srv-1", "ns", manifest={"kind": "Pod"}, wait_timeout=30)
    core.create_namespaced_pod.assert_not_awaited()


# ---------------------------------------------------------------------------
# _create_pod_with_retries
# ---------------------------------------------------------------------------


def _api_error(status, reason, message="x"):
    import json as _json

    ex = ApiException(status=status, reason=reason)
    ex.body = _json.dumps({"kind": "Status", "reason": reason, "message": message, "code": status})
    return ex


async def test_create_pod_retries_resource_quota_conflict(mocker):
    core, _ = _patch_core(mocker)
    created = _pod()
    conflict = _api_error(409, "Conflict", "Operation cannot be fulfilled on resourcequotas")
    core.create_namespaced_pod = mocker.AsyncMock(side_effect=[conflict, conflict, created])

    assert await kc._create_pod_with_retries(core, {"kind": "Pod"}, "srv-1", "ns") is created
    assert core.create_namespaced_pod.await_count == 3
    assert kc.sleep.await_count == 2


async def test_create_pod_already_exists_returns_the_existing_pod(mocker):
    core, _ = _patch_core(mocker)
    existing = _pod()
    core.create_namespaced_pod = mocker.AsyncMock(side_effect=_api_error(409, "AlreadyExists"))
    core.read_namespaced_pod = mocker.AsyncMock(return_value=existing)

    assert await kc._create_pod_with_retries(core, {"kind": "Pod"}, "srv-1", "ns") is existing
    assert core.create_namespaced_pod.await_count == 1
    core.read_namespaced_pod.assert_awaited_once_with(name="srv-1", namespace="ns")


async def test_create_pod_does_not_retry_client_errors(mocker):
    core, _ = _patch_core(mocker)
    core.create_namespaced_pod = mocker.AsyncMock(side_effect=_api_error(400, "BadRequest"))

    with pytest.raises(ApiException):
        await kc._create_pod_with_retries(core, {"kind": "Pod"}, "srv-1", "ns")
    assert core.create_namespaced_pod.await_count == 1


async def test_create_pod_gives_up_after_the_retry_budget(mocker):
    core, _ = _patch_core(mocker)
    core.create_namespaced_pod = mocker.AsyncMock(side_effect=_api_error(503, "ServiceUnavailable"))

    with pytest.raises(ApiException):
        await kc._create_pod_with_retries(core, {"kind": "Pod"}, "srv-1", "ns")
    assert core.create_namespaced_pod.await_count == kc._CREATE_RETRY_ATTEMPTS


async def test_deploy_server_creates_through_the_retrying_helper(mocker):
    core, apps = _patch_core(mocker)
    from kubernetes_asyncio.client import ApiClient

    api_client = ApiClient()
    apps.api_client = api_client
    try:
        created = _pod()
        conflict = _api_error(409, "Conflict", "Operation cannot be fulfilled on resourcequotas")
        core.create_namespaced_pod = mocker.AsyncMock(side_effect=[conflict, created])

        pod, manifest = await kc.deploy_server(image_tag="img:latest", server_name="srv-1", namespace="ns")

        assert pod is created
        assert manifest["metadata"]["name"] == "srv-1"
        assert core.create_namespaced_pod.await_count == 2
    finally:
        await api_client.close()
