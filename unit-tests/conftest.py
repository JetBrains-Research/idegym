"""Fixtures shared by the unit tests that inspect what ``deploy_server`` sends to Kubernetes."""

from types import SimpleNamespace

import pytest
from idegym.backend.utils import kubernetes_client as kc
from kubernetes_asyncio.client import ApiClient


@pytest.fixture
async def api_client():
    """A real ``ApiClient``, used only for its offline camelCase (de)serializer."""
    client = ApiClient()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def kube_clients(mocker, api_client) -> SimpleNamespace:
    """Patch ``create_clients`` with mocks, so the objects ``deploy_server`` creates can be read back.

    ``create_clients`` is used directly and by ``async_kube_api``; the bodies land on the
    ``apps``/``core``/``policy`` ``create_namespaced_*`` mocks.
    """
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
    return SimpleNamespace(apps=apps, core=core, policy=policy)
