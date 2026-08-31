"""What a readiness timeout says the pod was doing.

The bug this addresses is a *message* bug: "not ready in 60s" reads as a broken health endpoint
even when the image was still being pulled. So the assertions are about wording, and about the
default that made the timeout fire so early in the first place.
"""

from types import SimpleNamespace

import pytest
from idegym.api.config import SchedulingConfig
from idegym.api.orchestrator.servers import RestartServerRequest, StartServerRequest
from idegym.api.type import Duration
from idegym.backend.utils import kubernetes_client as kc


def _pod(phase="Pending", *, waiting=(), terminating=False, containers=1):
    statuses = [
        SimpleNamespace(
            state=SimpleNamespace(
                waiting=SimpleNamespace(reason=reason) if reason else None,
                terminated=None,
                running=None,
            )
        )
        for reason in (list(waiting) or [None] * containers)
    ]
    return SimpleNamespace(
        metadata=SimpleNamespace(name="pod", deletion_timestamp=object() if terminating else None),
        status=SimpleNamespace(phase=phase, container_statuses=statuses),
    )


@pytest.fixture
def pods(mocker):
    def configure(*items, error=None):
        listing = mocker.AsyncMock(side_effect=error) if error else mocker.AsyncMock(return_value=list(items))
        return mocker.patch.object(kc, "list_pods", listing)

    return configure


# --------------------------------------------------------------------------------------
# The diagnosis
# --------------------------------------------------------------------------------------


async def test_a_pull_in_progress_is_named_as_such(pods) -> None:
    pods(_pod(waiting=["ContainerCreating"]))

    assert "still pulling the image" in await kc.describe_pod_startup("app=srv", "ns")


async def test_a_failed_pull_is_distinguished_from_a_slow_one(pods) -> None:
    pods(_pod(waiting=["ImagePullBackOff"]))

    summary = await kc.describe_pod_startup("app=srv", "ns")
    assert "could not be pulled" in summary
    assert "still pulling" not in summary


async def test_a_running_container_points_at_the_readiness_probe(pods) -> None:
    pods(_pod("Running"))

    assert "readiness probe has not passed" in await kc.describe_pod_startup("app=srv", "ns")


async def test_no_pods_at_all_says_so(pods) -> None:
    pods()

    assert await kc.describe_pod_startup("app=srv", "ns") == "no pods matched"


async def test_a_terminating_pod_is_not_the_one_reported(pods) -> None:
    pods(_pod("Running", terminating=True), _pod(waiting=["ContainerCreating"]))

    assert "still pulling the image" in await kc.describe_pod_startup("app=srv", "ns")


async def test_an_unrecognised_waiting_reason_is_passed_through(pods) -> None:
    pods(_pod(waiting=["CreateContainerConfigError"]))

    assert "CreateContainerConfigError" in await kc.describe_pod_startup("app=srv", "ns")


async def test_a_failed_lookup_never_replaces_the_real_failure(pods) -> None:
    pods(error=RuntimeError("api server unreachable"))

    assert "pod state unavailable" in await kc.describe_pod_startup("app=srv", "ns")


# --------------------------------------------------------------------------------------
# The timeout that carries it
# --------------------------------------------------------------------------------------


async def test_the_readiness_timeout_reports_what_the_pod_was_doing(mocker, pods) -> None:
    mocker.patch.object(kc, "pods_are_ready", mocker.AsyncMock(return_value=(False, False, False, False)))
    pods(_pod(waiting=["ContainerCreating"]))

    with pytest.raises(TimeoutError, match="still pulling the image") as caught:
        await kc.wait_for_pods_ready(
            label_selector="app=srv",
            namespace="ns",
            wait_timeout=1,
            scheduling=SchedulingConfig(poll_interval=Duration(milliseconds=1)),
        )

    assert "were not ready within 1s" in str(caught.value)


# --------------------------------------------------------------------------------------
# The default that made it fire early
# --------------------------------------------------------------------------------------


def test_the_start_default_covers_a_cold_image_pull() -> None:
    assert StartServerRequest.model_fields["server_start_wait_timeout_in_seconds"].default == 300
    assert RestartServerRequest.model_fields["server_start_wait_timeout_in_seconds"].default == 300


def test_the_client_defaults_match_the_api() -> None:
    import inspect

    from idegym.client.client import IdeGYMClient

    for method in (IdeGYMClient.start_server, IdeGYMClient.with_server):
        signature = inspect.signature(method)
        assert signature.parameters["server_start_wait_timeout_in_seconds"].default == 300, method.__name__
