"""Unit tests for ``scripts/rollback.py``.

The cluster interaction is mocked; what is worth pinning down is the decision-making — how
releases and Deployments are resolved, what the downgrade Job inherits from the live
orchestrator, and the ordering guarantee that a failed downgrade never reaches Helm.
"""

import pytest

from scripts.rollback import (
    RollbackError,
    build_migration_job,
    declared_schema_revision,
    find_history_entry,
    main,
    orchestrator_container,
    release_deployments,
)

HISTORY = [
    {"revision": 6, "status": "superseded", "chart": "idegym-0.10.0"},
    {"revision": 7, "status": "deployed", "chart": "idegym-0.11.0"},
]

ORCHESTRATOR_DEPLOYMENT = {
    "metadata": {"name": "idegym"},
    "spec": {
        "replicas": 4,
        "template": {
            "spec": {
                "serviceAccountName": "idegym",
                "imagePullSecrets": [{"name": "ghcr"}],
                "tolerations": [{"key": "jetbrains.com/idegym", "operator": "Exists", "effect": "NoSchedule"}],
                "affinity": {"nodeAffinity": {}},
                "containers": [
                    {
                        "name": "orchestrator",
                        "image": "ghcr.io/jetbrains-research/idegym/orchestrator:0.11.0",
                        "imagePullPolicy": "IfNotPresent",
                        "env": [{"name": "POSTGRES_HOST", "value": "postgres"}],
                        "readinessProbe": {"httpGet": {"path": "/health", "port": "http"}},
                    },
                    {"name": "sidecar", "image": "busybox"},
                ],
            }
        },
    },
}

WATCHER_DEPLOYMENT = {
    "metadata": {"name": "idegym-watcher"},
    "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "watcher", "image": "watcher:0.11.0"}]}}},
}


def test_find_history_entry_returns_the_requested_revision():
    assert find_history_entry(HISTORY, 6)["chart"] == "idegym-0.10.0"


def test_find_history_entry_lists_what_exists_when_the_revision_does_not():
    with pytest.raises(RollbackError, match="have: 6, 7"):
        find_history_entry(HISTORY, 9)


def test_declared_schema_revision_reads_the_chart_value():
    assert declared_schema_revision({"database": {"schemaRevision": "002"}}) == "002"


@pytest.mark.parametrize("values", [{}, {"database": {}}, {"database": {"schemaRevision": ""}}])
def test_a_release_without_a_declared_revision_must_be_told_the_target(values):
    """Guessing the schema of a release that predates the declaration would be a data risk."""
    with pytest.raises(RollbackError, match="--target-schema-revision"):
        declared_schema_revision(values)


def test_release_deployments_finds_both_writers(mocker):
    mocker.patch("scripts.rollback.run_json", return_value={"items": [WATCHER_DEPLOYMENT, ORCHESTRATOR_DEPLOYMENT]})

    orchestrator, watcher = release_deployments("idegym", "idegym")

    assert orchestrator["metadata"]["name"] == "idegym"
    assert watcher["metadata"]["name"] == "idegym-watcher"


def test_release_deployments_tolerates_a_disabled_watcher(mocker):
    mocker.patch("scripts.rollback.run_json", return_value={"items": [ORCHESTRATOR_DEPLOYMENT]})

    orchestrator, watcher = release_deployments("idegym", "idegym")

    assert orchestrator["metadata"]["name"] == "idegym"
    assert watcher is None


def test_release_deployments_reports_what_it_found_when_ambiguous(mocker):
    mocker.patch("scripts.rollback.run_json", return_value={"items": []})

    with pytest.raises(RollbackError, match="found: none"):
        release_deployments("idegym", "idegym")


def test_orchestrator_container_is_picked_by_name():
    assert orchestrator_container(ORCHESTRATOR_DEPLOYMENT)["image"].endswith("orchestrator:0.11.0")


def test_orchestrator_container_missing_is_an_error():
    deployment = {"metadata": {"name": "idegym"}, "spec": {"template": {"spec": {"containers": []}}}}

    with pytest.raises(RollbackError, match="no orchestrator container"):
        orchestrator_container(deployment)


def test_migration_job_runs_the_live_image_and_environment():
    """The target release's image predates the migrations being reverted, so the Job must
    use the one currently deployed."""
    job = build_migration_job("idegym-migrate-002", "idegym", ORCHESTRATOR_DEPLOYMENT, "002", 600)

    container = job["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/jetbrains-research/idegym/orchestrator:0.11.0"
    assert container["env"] == [{"name": "POSTGRES_HOST", "value": "postgres"}]
    assert container["command"][-3:] == ["--target", "002", "--allow-downgrade"]
    assert "readinessProbe" not in container


def test_migration_job_inherits_scheduling_and_identity():
    job = build_migration_job("idegym-migrate-002", "idegym", ORCHESTRATOR_DEPLOYMENT, "002", 600)

    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "idegym"
    assert pod_spec["tolerations"][0]["key"] == "jetbrains.com/idegym"
    assert pod_spec["imagePullSecrets"] == [{"name": "ghcr"}]
    assert pod_spec["restartPolicy"] == "Never"
    # The sidecar is deliberately dropped: this pod only migrates.
    assert [container["name"] for container in pod_spec["containers"]] == ["migrate"]


def test_migration_job_is_not_retried():
    """A retry would run the downgrade again against a half-reverted schema."""
    job = build_migration_job("idegym-migrate-002", "idegym", ORCHESTRATOR_DEPLOYMENT, "002", 600)

    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 600


def fake_run_json(command: list[str]):
    """Answer the two read-only queries ``main`` makes: release history and Deployments."""
    if "history" in command:
        return HISTORY
    name = command[3]
    replicas = 1 if name.endswith("-watcher") else 4
    return {"metadata": {"name": name}, "spec": {"replicas": replicas}, "status": {"readyReplicas": replicas}}


@pytest.fixture
def cluster(mocker):
    """Patch away every cluster call so ``main`` can be driven end to end.

    Deployed release is Helm revision 7 at schema 003; revision 6 declared schema 002.
    """
    mocker.patch("scripts.rollback.current_helm_revision", return_value=7)
    mocker.patch("scripts.rollback.run_json", side_effect=fake_run_json)
    mocker.patch(
        "scripts.rollback.release_values",
        side_effect=lambda release, namespace, revision=None: {
            "database": {"schemaRevision": "002" if revision == 6 else "003"}
        },
    )
    mocker.patch("scripts.rollback.release_deployments", return_value=(ORCHESTRATOR_DEPLOYMENT, WATCHER_DEPLOYMENT))
    mocker.patch("scripts.rollback.exec_in_deployment", return_value=(0, "003"))
    mocker.patch("scripts.rollback.wait_for_no_pods")
    mocker.patch("scripts.rollback.create_job")
    mocker.patch("scripts.rollback.delete_job")
    return mocker


def test_a_failed_downgrade_never_reaches_helm(cluster, capsys):
    """The ordering guarantee of the whole script.

    Rolling the Kubernetes resources back on top of a half-reverted schema would leave the
    old code facing a schema it cannot read, with no record of what went wrong.
    """
    run = cluster.patch("scripts.rollback.run", return_value="")
    cluster.patch("scripts.rollback.scale")
    # The preflight Job succeeds; the downgrade that follows it does not.
    cluster.patch("scripts.rollback.wait_for_job", side_effect=[True, False])

    assert main(["--release", "idegym", "--namespace", "idegym", "--revision", "6", "--yes"]) == 1
    assert not [call for call in run.call_args_list if "rollback" in call.args[0]]
    assert "still stopped" in capsys.readouterr().err


def test_a_successful_downgrade_stops_the_writers_first(cluster):
    run = cluster.patch("scripts.rollback.run", return_value="")
    scale = cluster.patch("scripts.rollback.scale")
    cluster.patch("scripts.rollback.wait_for_job", return_value=True)

    assert main(["--release", "idegym", "--namespace", "idegym", "--revision", "6", "--yes"]) == 0
    assert [call.args for call in scale.call_args_list] == [("idegym", "idegym", 0), ("idegym-watcher", "idegym", 0)]
    assert [call for call in run.call_args_list if "rollback" in call.args[0]]


def test_the_preflight_runs_before_any_writer_stops(cluster):
    """Ordering: the source image is asked whether it can migrate while it is still serving."""
    calls: list[str] = []
    cluster.patch("scripts.rollback.create_job", side_effect=lambda job: calls.append(job["metadata"]["name"]))
    cluster.patch("scripts.rollback.scale", side_effect=lambda *_: calls.append("scale"))
    cluster.patch("scripts.rollback.run", return_value="")
    cluster.patch("scripts.rollback.wait_for_job", return_value=True)

    assert main(["--release", "idegym", "--namespace", "idegym", "--revision", "6", "--yes"]) == 0
    assert calls == ["idegym-migrate-check-002", "scale", "scale", "idegym-migrate-002"]


def test_a_matching_schema_rolls_back_without_stopping_anything(cluster, capsys):
    """An upgrade that did not change the schema needs no downgrade and no maintenance window."""
    run = cluster.patch("scripts.rollback.run", return_value="")
    cluster.patch(
        "scripts.rollback.release_values",
        side_effect=lambda release, namespace, revision=None: {"database": {"schemaRevision": "003"}},
    )
    scale = cluster.patch("scripts.rollback.scale")

    assert main(["--release", "idegym", "--namespace", "idegym", "--revision", "6"]) == 0
    assert [call for call in run.call_args_list if "rollback" in call.args[0]]
    scale.assert_not_called()
    assert "schema was already at 003" in capsys.readouterr().out


def test_dry_run_touches_neither_the_release_nor_the_writers(cluster):
    """Only the preflight runs, and the Job it uses to ask the image is cleaned up."""
    run = cluster.patch("scripts.rollback.run", return_value="")
    scale = cluster.patch("scripts.rollback.scale")
    create_job = cluster.patch("scripts.rollback.create_job")
    delete_job = cluster.patch("scripts.rollback.delete_job")
    cluster.patch("scripts.rollback.wait_for_job", return_value=True)

    assert main(["--release", "idegym", "--namespace", "idegym", "--revision", "6", "--dry-run"]) == 0
    run.assert_not_called()
    scale.assert_not_called()
    assert create_job.call_count == 1
    assert create_job.call_args.args[0]["spec"]["template"]["spec"]["containers"][0]["command"][-1] == "--dry-run"
    delete_job.assert_called_once()


def test_a_source_image_that_cannot_migrate_aborts_before_anything_stops(cluster, capsys):
    """The preflight is what makes rolling back with the wrong image a no-op, not a mess."""
    cluster.patch("scripts.rollback.wait_for_job", return_value=False)
    scale = cluster.patch("scripts.rollback.scale")
    run = cluster.patch("scripts.rollback.run", return_value="")

    assert main(["--release", "idegym", "--namespace", "idegym", "--revision", "6", "--yes"]) == 1
    scale.assert_not_called()
    run.assert_not_called()
    assert "cannot migrate to 002" in capsys.readouterr().err


def test_rolling_back_to_the_current_revision_is_refused(cluster, capsys):
    assert main(["--release", "idegym", "--namespace", "idegym", "--revision", "7"]) == 1
    assert "already at revision 7" in capsys.readouterr().err
