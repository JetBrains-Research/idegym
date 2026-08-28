"""Unit tests for the node-holder success/failure signalling.

``change_number_of_spun_nodes`` returns an *error* flag that callers turn into
``AvailabilityStatus.DELETION_FAILED`` (``watcher/cleanup.py``, ``router/client.py``), so both
node-holder helpers it calls must report a happy path as a happy path. The Kubernetes API is
mocked; nothing here touches a cluster.
"""

from types import SimpleNamespace
from uuid import uuid4

from idegym.backend.utils import kubernetes_client as kc
from idegym.orchestrator import nodes_holder as nh


def _patch_clients(mocker):
    deployment_result = mocker.MagicMock()
    deployment_result.api_version = "apps/v1"
    deployment_result.kind = "Deployment"
    deployment_result.metadata.name = "node-holder-hash"
    deployment_result.metadata.uid = "uid-123"

    apps = mocker.MagicMock()
    apps.create_namespaced_deployment = mocker.AsyncMock(return_value=deployment_result)
    policy = mocker.MagicMock()
    policy.create_namespaced_pod_disruption_budget = mocker.AsyncMock()

    clients = (apps, mocker.MagicMock(), mocker.MagicMock(), policy, mocker.MagicMock())
    mocker.patch.object(kc, "create_clients", mocker.AsyncMock(return_value=clients))
    return apps


async def test_spin_up_reports_success(mocker):
    _patch_clients(mocker)
    mocker.patch.object(nh, "wait_for_pods_ready", mocker.AsyncMock())

    assert await nh.spin_up_or_update_nodes_for_client(client_name="c", nodes_count=2, namespace="ns") is True


async def test_spin_up_reports_success_without_readiness_wait(mocker):
    _patch_clients(mocker)
    wait_for_pods_ready = mocker.patch.object(nh, "wait_for_pods_ready", mocker.AsyncMock())

    result = await nh.spin_up_or_update_nodes_for_client(client_name="c", nodes_count=2, namespace="ns", wait_timeout=0)

    assert result is True
    wait_for_pods_ready.assert_not_awaited()


async def test_spin_up_reports_success_when_no_nodes_requested(mocker):
    assert await nh.spin_up_or_update_nodes_for_client(client_name="c", nodes_count=0, namespace="ns") is True


async def test_change_number_of_spun_nodes_reports_no_error_on_successful_scale(mocker):
    _patch_clients(mocker)
    mocker.patch.object(
        nh,
        "need_to_release_nodes_for_client",
        mocker.AsyncMock(return_value=SimpleNamespace(name="c", nodes=2)),
    )

    assert await nh.change_number_of_spun_nodes(client_id=uuid4(), namespace="ns") is False


async def test_change_number_of_spun_nodes_reports_error_on_failed_scale(mocker):
    mocker.patch.object(
        nh,
        "need_to_release_nodes_for_client",
        mocker.AsyncMock(return_value=SimpleNamespace(name="c", nodes=2)),
    )
    mocker.patch.object(nh, "spin_up_or_update_nodes_for_client", mocker.AsyncMock(side_effect=RuntimeError("boom")))

    assert await nh.change_number_of_spun_nodes(client_id=uuid4(), namespace="ns") is True


async def test_change_number_of_spun_nodes_reports_no_error_on_successful_release(mocker):
    mocker.patch.object(
        nh,
        "need_to_release_nodes_for_client",
        mocker.AsyncMock(return_value=SimpleNamespace(name="c", nodes=0)),
    )
    mocker.patch.object(nh, "release_nodes_for_client", mocker.AsyncMock(return_value=True))

    assert await nh.change_number_of_spun_nodes(client_id=uuid4(), namespace="ns") is False


async def test_change_number_of_spun_nodes_reports_error_on_failed_release(mocker):
    mocker.patch.object(
        nh,
        "need_to_release_nodes_for_client",
        mocker.AsyncMock(return_value=SimpleNamespace(name="c", nodes=0)),
    )
    mocker.patch.object(nh, "release_nodes_for_client", mocker.AsyncMock(return_value=False))

    assert await nh.change_number_of_spun_nodes(client_id=uuid4(), namespace="ns") is True
