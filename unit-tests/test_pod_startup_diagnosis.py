"""What a readiness timeout says the pod was doing.

The bug this addresses is a *message* bug: "not ready in 60s" reads as a broken health endpoint
even when the image was still being pulled. So the assertions are about wording, and about the
default that made the timeout fire so early in the first place.
"""

import asyncio
from types import SimpleNamespace

import pytest
from idegym.api.config import SchedulingConfig
from idegym.api.orchestrator.servers import RestartServerRequest, StartServerRequest
from idegym.api.type import Duration
from idegym.backend.utils import kubernetes_client as kc


def _pod(phase="Pending", *, waiting=(), terminating=False, containers=1, ready=False, init=()):
    statuses = [
        SimpleNamespace(
            name="server",
            ready=ready,
            state=SimpleNamespace(
                waiting=SimpleNamespace(reason=reason) if reason else None,
                terminated=None,
                running=None,
            ),
        )
        for reason in (list(waiting) or [None] * containers)
    ]
    return SimpleNamespace(
        metadata=SimpleNamespace(name="pod", deletion_timestamp=object() if terminating else None),
        status=SimpleNamespace(phase=phase, container_statuses=statuses, init_container_statuses=list(init)),
    )


def _init(name="setup", *, waiting=None, exit_code=None, reason=None, running=False, ready=False):
    return SimpleNamespace(
        name=name,
        ready=ready,
        state=SimpleNamespace(
            waiting=SimpleNamespace(reason=waiting) if waiting else None,
            terminated=SimpleNamespace(exit_code=exit_code, reason=reason) if exit_code is not None else None,
            running=SimpleNamespace() if running else None,
        ),
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


async def test_the_pod_holding_the_wait_up_is_the_one_diagnosed(pods) -> None:
    """With several pods, a ready one listed first must not hide the one still pulling."""
    pods(_pod("Running", ready=True), _pod(waiting=["ContainerCreating"]))

    assert await kc.describe_pod_startup("app=srv", "ns") == (
        "1/2 pods ready; still pulling the image or creating the container (ContainerCreating)"
    )


async def test_a_single_pod_is_described_without_a_count(pods) -> None:
    pods(_pod(waiting=["ContainerCreating"]))

    assert "pods ready" not in await kc.describe_pod_startup("app=srv", "ns")


async def test_a_ready_pod_waiting_on_an_old_one_is_not_blamed_on_its_probe(pods) -> None:
    """A RESTART reuse whose old pod is stuck terminating: the new pod is fine."""
    pods(_pod("Running", terminating=True), _pod("Running", ready=True))

    summary = await kc.describe_pod_startup("app=srv", "ns")
    assert summary == "new pod ready, waiting for 1 old pod(s) to terminate"
    assert "readiness probe" not in summary


async def test_the_readiness_probe_is_blamed_only_on_a_container_that_is_not_ready(pods) -> None:
    pods(_pod("Running", ready=False))

    assert await kc.describe_pod_startup("app=srv", "ns") == (
        "image pulled and container running, but its readiness probe has not passed (not ready: server)"
    )


async def test_a_crash_looping_init_container_is_named_instead_of_a_pull(pods) -> None:
    """The main container waits with PodInitializing, which alone reads as a pull in progress."""
    pods(_pod(waiting=["PodInitializing"], init=[_init("migrate", waiting="CrashLoopBackOff")]))

    summary = await kc.describe_pod_startup("app=srv", "ns")
    assert summary == "init container 'migrate' waiting (CrashLoopBackOff)"
    assert "still pulling" not in summary


async def test_a_failed_init_container_reports_its_exit_code(pods) -> None:
    pods(_pod(waiting=["PodInitializing"], init=[_init("migrate", exit_code=1, reason="Error")]))

    assert await kc.describe_pod_startup("app=srv", "ns") == "init container 'migrate' failed (exit code 1, Error)"


async def test_a_running_init_container_is_named(pods) -> None:
    pods(_pod(waiting=["PodInitializing"], init=[_init("done", exit_code=0), _init("fetch", running=True)]))

    assert await kc.describe_pod_startup("app=srv", "ns") == "init container 'fetch' still running"


async def test_completed_init_containers_leave_the_main_container_diagnosis(pods) -> None:
    pods(_pod(waiting=["ContainerCreating"], init=[_init("setup", exit_code=0, reason="Completed", ready=True)]))

    assert "still pulling the image or creating the container" in await kc.describe_pod_startup("app=srv", "ns")


async def test_an_init_container_image_that_cannot_be_pulled_is_named(pods) -> None:
    pods(_pod(waiting=["PodInitializing"], init=[_init("setup", waiting="ImagePullBackOff")]))

    assert "the image of init container 'setup' could not be pulled" in await kc.describe_pod_startup("app=srv", "ns")


async def test_an_unrecognised_waiting_reason_is_passed_through(pods) -> None:
    pods(_pod(waiting=["CreateContainerConfigError"]))

    assert "CreateContainerConfigError" in await kc.describe_pod_startup("app=srv", "ns")


async def test_a_failed_lookup_never_replaces_the_real_failure(pods) -> None:
    pods(error=RuntimeError("api server unreachable"))

    assert "pod state unavailable" in await kc.describe_pod_startup("app=srv", "ns")


async def test_a_hanging_lookup_is_bounded(mocker) -> None:
    """The API server is often the reason the wait expired; the diagnostic must not hang on it too."""

    async def hang(*_):
        await asyncio.sleep(3600)

    mocker.patch.object(kc, "_DIAGNOSIS_TIMEOUT_SECONDS", 0.01)
    mocker.patch.object(kc, "list_pods", hang)

    assert await kc.describe_pod_startup("app=srv", "ns") == "pod state unavailable: TimeoutError"


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
