"""Unit tests for the watcher crash detector.

``evaluate_pod_crash`` / ``_index_pods_by_app`` are pure and tested against fabricated,
duck-typed pod objects (``SimpleNamespace``) so no Kubernetes models are required.
``detect_crashed_servers`` is tested with every Kubernetes/database helper it imports mocked
in the ``idegym.watcher.crash_detector`` namespace.
"""

from types import SimpleNamespace

import pytest
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.watcher.crash_detector import (
    _index_pods_by_app,
    detect_crashed_servers,
    evaluate_pod_crash,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Pod fabrication helpers
# ---------------------------------------------------------------------------


def _terminated(reason=None, exit_code=None, signal=None, message=None):
    return SimpleNamespace(reason=reason, exit_code=exit_code, signal=signal, message=message)


def _waiting(reason=None, message=None):
    return SimpleNamespace(reason=reason, message=message)


def _container(*, restart_count=0, last_terminated=None, cur_terminated=None, waiting=None):
    return SimpleNamespace(
        restart_count=restart_count,
        state=SimpleNamespace(terminated=cur_terminated, waiting=waiting, running=None),
        last_state=SimpleNamespace(terminated=last_terminated),
    )


def _pod(
    generated_name="srv-1",
    *,
    phase="Running",
    reason=None,
    message=None,
    container_statuses=None,
    deletion_timestamp=None,
    labels=None,
):
    if labels is None:
        labels = {"app": generated_name, "app.kubernetes.io/part-of": "idegym"}
    return SimpleNamespace(
        metadata=SimpleNamespace(deletion_timestamp=deletion_timestamp, labels=labels, name=generated_name),
        status=SimpleNamespace(
            phase=phase, reason=reason, message=message, container_statuses=container_statuses or []
        ),
    )


# ---------------------------------------------------------------------------
# evaluate_pod_crash
# ---------------------------------------------------------------------------


def test_healthy_running_pod_is_not_a_crash():
    pod = _pod(container_statuses=[_container(restart_count=0)])
    assert evaluate_pod_crash(pod, max_restarts=0) is None


def test_oom_killed_restart_over_budget():
    pod = _pod(
        container_statuses=[_container(restart_count=1, last_terminated=_terminated(reason="OOMKilled", exit_code=137))]
    )
    reason = evaluate_pod_crash(pod, max_restarts=0)
    assert reason is not None
    assert "restarted 1 time(s)" in reason
    assert "budget of 0" in reason
    assert "OOMKilled" in reason
    assert "exit 137" in reason


def test_restart_within_budget_is_not_a_crash():
    pod = _pod(
        container_statuses=[_container(restart_count=1, last_terminated=_terminated(reason="Error", exit_code=1))]
    )
    assert evaluate_pod_crash(pod, max_restarts=3) is None


def test_restart_exceeding_nonzero_budget():
    pod = _pod(
        container_statuses=[_container(restart_count=5, last_terminated=_terminated(reason="Error", exit_code=1))]
    )
    reason = evaluate_pod_crash(pod, max_restarts=3)
    assert reason is not None
    assert "restarted 5 time(s)" in reason
    assert "Error" in reason


def test_crashloopbackoff_waiting_reason_is_surfaced():
    pod = _pod(
        container_statuses=[
            _container(
                restart_count=4,
                waiting=_waiting(reason="CrashLoopBackOff", message="back-off restarting failed container"),
            )
        ]
    )
    reason = evaluate_pod_crash(pod, max_restarts=0)
    assert reason is not None
    assert "CrashLoopBackOff" in reason


def test_evicted_pod_is_a_crash_regardless_of_budget():
    # Out-of-storage / disk pressure surfaces as a pod-level eviction with no container restart.
    pod = _pod(
        phase="Failed",
        reason="Evicted",
        message="The node was low on resource: ephemeral-storage.",
        container_statuses=[_container(restart_count=0)],
    )
    reason = evaluate_pod_crash(pod, max_restarts=100)
    assert reason is not None
    assert "evicted" in reason.lower()
    assert "ephemeral-storage" in reason


def test_failed_phase_without_reason():
    pod = _pod(phase="Failed", container_statuses=[_container(restart_count=0)])
    reason = evaluate_pod_crash(pod, max_restarts=0)
    assert reason is not None
    assert "failed" in reason.lower()


def test_terminating_pod_is_ignored():
    pod = _pod(
        deletion_timestamp="2026-06-12T00:00:00Z",
        container_statuses=[
            _container(restart_count=9, last_terminated=_terminated(reason="OOMKilled", exit_code=137))
        ],
    )
    assert evaluate_pod_crash(pod, max_restarts=0) is None


def test_missing_status_is_ignored():
    pod = SimpleNamespace(metadata=SimpleNamespace(deletion_timestamp=None, labels={}), status=None)
    assert evaluate_pod_crash(pod, max_restarts=0) is None


# ---------------------------------------------------------------------------
# _index_pods_by_app
# ---------------------------------------------------------------------------


def test_index_pods_by_app_skips_unlabeled_and_prefers_live():
    live = _pod("srv-1")
    terminating = _pod("srv-1", deletion_timestamp="2026-06-12T00:00:00Z")
    unlabeled = _pod("srv-2", labels={})

    # Terminating listed first, live second -> live wins.
    indexed = _index_pods_by_app([terminating, live, unlabeled])
    assert indexed["srv-1"] is live
    assert "srv-2" not in indexed


# ---------------------------------------------------------------------------
# detect_crashed_servers
# ---------------------------------------------------------------------------


def _server(server_id, generated_name, *, namespace="idegym", max_restarts=0):
    return SimpleNamespace(id=server_id, generated_name=generated_name, namespace=namespace, max_restarts=max_restarts)


async def test_detect_marks_crashed_and_tears_down(mocker):
    crashing = _server(1, "srv-1")
    healthy = _server(2, "srv-2")

    mocker.patch(
        "idegym.watcher.crash_detector.get_idegym_servers_by_status",
        new=mocker.AsyncMock(return_value=[crashing, healthy]),
    )
    list_pods = mocker.patch(
        "idegym.watcher.crash_detector.list_pods",
        new=mocker.AsyncMock(
            return_value=[
                _pod(
                    "srv-1",
                    container_statuses=[_container(restart_count=2, last_terminated=_terminated("OOMKilled", 137))],
                ),
                _pod("srv-2", container_statuses=[_container(restart_count=0)]),
            ]
        ),
    )
    heartbeat = mocker.patch("idegym.watcher.crash_detector.update_idegym_server_heartbeat", new=mocker.AsyncMock())
    clean_up = mocker.patch("idegym.watcher.crash_detector.clean_up_server", new=mocker.AsyncMock())

    await detect_crashed_servers(db=mocker.MagicMock())

    # Single list call for the one namespace, covering both servers.
    list_pods.assert_awaited_once()
    # Only the crashing server is marked CRASHED + torn down.
    heartbeat.assert_awaited_once()
    args, kwargs = heartbeat.await_args
    assert args[1] == 1  # server_id
    assert args[2] == AvailabilityStatus.CRASHED
    assert "OOMKilled" in kwargs["details"]
    clean_up.assert_awaited_once_with(name="srv-1", namespace="idegym")


async def test_detect_one_list_per_namespace(mocker):
    servers = [
        _server(1, "a-1", namespace="ns-a"),
        _server(2, "a-2", namespace="ns-a"),
        _server(3, "b-1", namespace="ns-b"),
    ]
    mocker.patch(
        "idegym.watcher.crash_detector.get_idegym_servers_by_status",
        new=mocker.AsyncMock(return_value=servers),
    )
    list_pods = mocker.patch("idegym.watcher.crash_detector.list_pods", new=mocker.AsyncMock(return_value=[]))
    mocker.patch("idegym.watcher.crash_detector.update_idegym_server_heartbeat", new=mocker.AsyncMock())
    mocker.patch("idegym.watcher.crash_detector.clean_up_server", new=mocker.AsyncMock())

    await detect_crashed_servers(db=mocker.MagicMock())

    # Two distinct namespaces -> exactly two list calls, never one per server.
    assert list_pods.await_count == 2
    assert {call.args[1] for call in list_pods.await_args_list} == {"ns-a", "ns-b"}


async def test_detect_teardown_failure_does_not_raise(mocker):
    crashing = _server(1, "srv-1")
    mocker.patch(
        "idegym.watcher.crash_detector.get_idegym_servers_by_status",
        new=mocker.AsyncMock(return_value=[crashing]),
    )
    mocker.patch(
        "idegym.watcher.crash_detector.list_pods",
        new=mocker.AsyncMock(
            return_value=[
                _pod("srv-1", container_statuses=[_container(restart_count=1, last_terminated=_terminated("Error", 1))])
            ]
        ),
    )
    heartbeat = mocker.patch("idegym.watcher.crash_detector.update_idegym_server_heartbeat", new=mocker.AsyncMock())
    mocker.patch(
        "idegym.watcher.crash_detector.clean_up_server",
        new=mocker.AsyncMock(side_effect=RuntimeError("boom")),
    )

    # Must not raise: when teardown fails the server is recorded DELETION_FAILED, still with the reason.
    await detect_crashed_servers(db=mocker.MagicMock())
    heartbeat.assert_awaited_once()
    args, kwargs = heartbeat.await_args
    assert args[2] == AvailabilityStatus.DELETION_FAILED
    assert "Error" in kwargs["details"]
