"""End-to-end coverage for release rollback, schema included.

Exercises the two halves of `scripts/rollback.py` against a real cluster and a real
release: the exact-revision schema move it performs with the deployed image and every
writer stopped, and the plain application-only rollback it falls back to when the target
release declares the same schema.

The literal N -> N+1 -> N case cannot be staged here, because a single-image cluster has
only one set of migrations: an orchestrator always migrates to its own head on startup, so
no release in the history can hold the schema below it. What is covered instead is every
mechanism that case relies on — the declared revision, the downgrade Job built from the
live Deployment, the round trip with data in place, and the Helm handover.
"""

import json
import subprocess

from alembic.config import Config
from alembic.script import ScriptDirectory
from from_root import from_root
from idegym.utils.logging import get_logger
from utils import k8s_client
from utils.constants import (
    CHART_PATH,
    DEFAULT_NAMESPACE,
    HELM_RELEASE,
    POSTGRESQL_APP_LABEL,
    POSTGRESQL_DB,
    POSTGRESQL_USER,
    WATCHER_APP_LABEL,
)
from utils.k8s_setup import wait_for_pod_ready, wait_for_service

from scripts.rollback import (
    MIGRATION_CLI,
    build_migration_job,
    create_job,
    current_helm_revision,
    declared_schema_revision,
    delete_job,
    exec_in_deployment,
    main,
    migration_job_name,
    release_deployments,
    release_values,
    replica_count,
    scale,
    wait_for_job,
    wait_for_no_pods,
)

logger = get_logger(__name__)

ALEMBIC_INI = from_root("orchestrator", "src", "idegym", "orchestrator", "alembic.ini")
STEP_TIMEOUT_SECONDS = 300
_PG_SUPERUSER_PASSWORD = "postgres"


def revision_chain() -> list[str]:
    """Revision ids base -> head, read from the same scripts the deployed image contains."""
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    return [revision.revision for revision in script.walk_revisions()][::-1]


def psql(sql: str) -> str:
    pod_names = k8s_client.list_pod_names(
        namespace=DEFAULT_NAMESPACE, label_selector=f"app.kubernetes.io/name={POSTGRESQL_APP_LABEL}"
    )
    if not pod_names:
        raise RuntimeError(f"No postgresql pod found in namespace {DEFAULT_NAMESPACE}")
    return k8s_client.exec_in_pod(
        pod_name=pod_names[0],
        namespace=DEFAULT_NAMESPACE,
        command=[
            "env",
            f"PGPASSWORD={_PG_SUPERUSER_PASSWORD}",
            "psql",
            "-U",
            POSTGRESQL_USER,
            "-d",
            POSTGRESQL_DB,
            "-tAc",
            sql,
        ],
    ).strip()


def cli_output(deployment_name: str, arguments: list[str]) -> str:
    """Run the migration CLI in a live pod and return the value it printed.

    The container logs structured JSON to stdout too, and the command prints its answer
    last, so the value is the final non-empty line of stdout.
    """
    code, stdout, stderr = exec_in_deployment(deployment_name, DEFAULT_NAMESPACE, [*MIGRATION_CLI, *arguments])
    assert code == 0, f"{' '.join(arguments)} failed: {stderr or stdout}"
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    assert lines, f"{' '.join(arguments)} printed nothing (stderr: {stderr})"
    return lines[-1]


def run_migration_job(orchestrator: dict, target: str) -> None:
    """Run the downgrade/upgrade Job `scripts/rollback.py` would run, and require success.

    ``build_migration_job`` always passes ``--allow-downgrade``; the CLI only consults it
    when the plan actually moves backwards, so the same builder serves both directions.
    """
    name = migration_job_name(orchestrator["metadata"]["name"], f"migrate-{target}")
    delete_job(name, DEFAULT_NAMESPACE)
    create_job(build_migration_job(name, DEFAULT_NAMESPACE, orchestrator, target, STEP_TIMEOUT_SECONDS))
    assert wait_for_job(name, DEFAULT_NAMESPACE, STEP_TIMEOUT_SECONDS), f"migration Job {name} did not succeed"


def seed_representative_rows(marker: str) -> None:
    psql(
        "WITH c AS ("
        "  INSERT INTO clients (id, name, namespace, created_at, last_heartbeat_time, availability, nodes_count)"
        f"  VALUES (gen_random_uuid(), '{marker}', '{DEFAULT_NAMESPACE}', 1, 1, 'ALIVE', 0)"
        "  RETURNING id"
        ") "
        "INSERT INTO servers (client_id, client_name, server_name, generated_name, namespace, created_at,"
        " last_heartbeat_time, availability, cpu, ram, run_as_root, server_kind, service_port) "
        f"SELECT id, '{marker}', 'srv', '{marker}', '{DEFAULT_NAMESPACE}', 1, 1, 'ALIVE',"
        " 0, 0, false, 'idegym', 80 FROM c;"
    )


def deployed_log_level() -> str:
    """The currently deployed release's log level — the marker this test toggles."""
    values = json.loads(
        subprocess.run(
            ["helm", "get", "values", HELM_RELEASE, "--namespace", DEFAULT_NAMESPACE, "--all", "--output", "json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    return values["deployment"]["log"]["level"]


def restore_writers(replicas_by_name: dict[str, int]) -> None:
    for name, replicas in replicas_by_name.items():
        scale(name, DEFAULT_NAMESPACE, replicas)
    assert wait_for_service(), "Orchestrator did not become healthy again"
    assert wait_for_pod_ready(WATCHER_APP_LABEL, timeout=STEP_TIMEOUT_SECONDS), "Watcher did not become ready again"


def test_release_declares_the_schema_revision_its_image_produces():
    """The invariant every exact rollback rests on.

    The chart value is what a rollback downgrades to, so a release whose declaration drifts
    from its image would send the next rollback to the wrong revision.
    """
    values = release_values(HELM_RELEASE, DEFAULT_NAMESPACE)
    declared = declared_schema_revision(values)
    orchestrator, _ = release_deployments(HELM_RELEASE, DEFAULT_NAMESPACE, values)
    orchestrator_name = orchestrator["metadata"]["name"]

    assert cli_output(orchestrator_name, ["schema", "head"]) == declared
    assert cli_output(orchestrator_name, ["schema", "current"]) == declared
    assert psql("SELECT version_num FROM alembic_version;") == declared


def test_schema_downgrades_and_comes_back_with_data_intact():
    """Walk the deployed schema back one revision and forward again, in-cluster.

    Rows are seeded into the tables revision 001 creates, so they survive any incremental
    revision's downgrade; if a future revision's downgrade drops them, that is a
    data-destroying downgrade this test should fail on.
    """
    chain = revision_chain()
    assert len(chain) >= 2, "need at least two revisions to exercise a downgrade"
    head, previous = chain[-1], chain[-2]

    orchestrator, watcher = release_deployments(
        HELM_RELEASE, DEFAULT_NAMESPACE, release_values(HELM_RELEASE, DEFAULT_NAMESPACE)
    )
    writers = [orchestrator, *([watcher] if watcher else [])]
    original_replicas = {deployment["metadata"]["name"]: replica_count(deployment) for deployment in writers}

    seed_representative_rows("rollback-e2e")
    assert psql("SELECT count(*) FROM servers WHERE generated_name = 'rollback-e2e';") == "1"

    try:
        for name in original_replicas:
            scale(name, DEFAULT_NAMESPACE, 0)
        for deployment in writers:
            wait_for_no_pods(deployment, DEFAULT_NAMESPACE, STEP_TIMEOUT_SECONDS)

        run_migration_job(orchestrator, previous)
        assert psql("SELECT version_num FROM alembic_version;") == previous
        assert psql("SELECT count(*) FROM servers WHERE generated_name = 'rollback-e2e';") == "1"

        run_migration_job(orchestrator, head)
        assert psql("SELECT version_num FROM alembic_version;") == head
        assert psql("SELECT count(*) FROM servers WHERE generated_name = 'rollback-e2e';") == "1"
    finally:
        restore_writers(original_replicas)


def test_application_only_rollback_leaves_the_schema_alone():
    """A rollback between releases that declare the same revision is a plain Helm rollback.

    No writer is stopped and no migration Job runs — the schema is already what the target
    release expects, which is the routine case an expand/contract migration policy aims for.
    """
    deployed = current_helm_revision(HELM_RELEASE, DEFAULT_NAMESPACE)
    subprocess.run(
        [
            "helm",
            "upgrade",
            HELM_RELEASE,
            str(CHART_PATH),
            "--namespace",
            DEFAULT_NAMESPACE,
            "--reuse-values",
            "--set",
            "deployment.log.level=debug",
            "--wait",
            "--timeout",
            f"{STEP_TIMEOUT_SECONDS}s",
        ],
        check=True,
    )
    assert current_helm_revision(HELM_RELEASE, DEFAULT_NAMESPACE) == deployed + 1

    try:
        exit_code = main(
            ["--release", HELM_RELEASE, "--namespace", DEFAULT_NAMESPACE, "--revision", str(deployed), "--yes"]
        )
        assert exit_code == 0
        assert deployed_log_level() != "debug", "the rollback did not restore the target release's values"
    finally:
        # A Helm rollback records a new revision carrying the old values, so the values are
        # what says whether the release is back where the session left it — not the number.
        if deployed_log_level() == "debug":
            subprocess.run(
                [
                    "helm",
                    "rollback",
                    HELM_RELEASE,
                    str(deployed),
                    "--namespace",
                    DEFAULT_NAMESPACE,
                    "--wait",
                    "--timeout",
                    f"{STEP_TIMEOUT_SECONDS}s",
                ],
                check=False,
            )
        assert wait_for_service(), "Orchestrator did not become healthy again"
