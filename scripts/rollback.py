#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Roll an IdeGYM release back, including its database schema.

``helm rollback`` restores Kubernetes resources only, and the orchestrator migrates forward
at startup, so a failed upgrade leaves the database on a revision the older image may not
contain at all. This script sequences what Helm cannot.

The shape of the rollback follows the ``database.schemaRevision`` each release declares.
Same revision: plain ``helm rollback``. Older revision: stop both writers, downgrade with
the *currently deployed* image — the only one holding the migrations being reverted — and
hand over to Helm only once that has succeeded.

A downgrade discards what the reverted revisions stored, and there is no automatic backup,
so take one first if the data matters::

    kubectl exec -n <namespace> <postgres-pod> -- \
        pg_dump -U <user> -Fc <database> > idegym-pre-rollback.dump

Needs ``helm`` and ``kubectl`` on PATH, with permission to scale Deployments, create Jobs,
and exec into pods.

Usage::

    scripts/rollback.py --release idegym --namespace idegym --revision 7 --dry-run
    scripts/rollback.py --release idegym --namespace idegym --revision 7
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from argparse import ArgumentParser, Namespace
from typing import Any, Optional

# The chart's name for this container; anything else in the pod is a sidecar.
ORCHESTRATOR_CONTAINER = "orchestrator"

# Subcharts share the release's instance label, so the chart's own Deployments are found by
# this one: `nameOverride | chart name`, plus the suffix for the watcher.
APP_NAME_LABEL = "app.kubernetes.io/name"
CHART_NAME = "idegym"
WATCHER_NAME_SUFFIX = "-watcher"

# A Job's name becomes a label value on its pods, so the label limit bounds it.
MAX_JOB_NAME_LENGTH = 63

# Inherited by the migration Job so it schedules and authenticates exactly like the
# orchestrator, instead of restating the release's configuration here.
INHERITED_POD_SPEC_KEYS = (
    "affinity",
    "imagePullSecrets",
    "nodeSelector",
    "securityContext",
    "serviceAccountName",
    "tolerations",
)

MIGRATION_CLI = ["uv", "run", "python", "-m", "idegym.orchestrator.db_cli"]

JOB_POLL_INTERVAL_SECONDS = 2


class RollbackError(RuntimeError):
    """A rollback step failed, or a precondition was not met."""


def run(command: list[str], *, capture: bool = True) -> str:
    """Run a command, raising :class:`RollbackError` with its stderr when it fails."""
    printable = " ".join(command)
    print(f"$ {printable}")
    result = subprocess.run(command, capture_output=capture, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise RollbackError(f"`{printable}` exited {result.returncode}{f': {detail}' if detail else ''}")
    return result.stdout if capture else ""


def run_json(command: list[str]) -> Any:
    output = run(command)
    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        raise RollbackError(f"`{' '.join(command)}` did not return JSON: {e}") from e


def helm(args: list[str], release: str, namespace: str) -> list[str]:
    return ["helm", *args, release, "--namespace", namespace]


def current_helm_revision(release: str, namespace: str) -> int:
    status = run_json([*helm(["status"], release, namespace), "--output", "json"])
    revision = status.get("version")
    if not isinstance(revision, int):
        raise RollbackError(f"Could not read the current revision of release {release}")
    return revision


def find_history_entry(history: list[dict[str, Any]], revision: int) -> dict[str, Any]:
    for entry in history:
        if entry.get("revision") == revision:
            return entry
    known = ", ".join(str(entry.get("revision")) for entry in history)
    raise RollbackError(f"Revision {revision} is not in the release history (have: {known})")


def declared_schema_revision(values: dict[str, Any], role: str = "target") -> str:
    """Read ``database.schemaRevision`` out of a release's computed values.

    Absent means the release predates the declaration, and an exact target cannot be
    guessed, so the operator has to supply it.
    """
    revision = (values.get("database") or {}).get("schemaRevision")
    if not revision:
        raise RollbackError(
            f"The {role} release does not declare database.schemaRevision, so its expected schema "
            "revision is unknown. Pass --target-schema-revision to state it explicitly"
        )
    return str(revision)


def release_values(release: str, namespace: str, revision: Optional[int] = None) -> dict[str, Any]:
    command = [*helm(["get", "values"], release, namespace), "--all", "--output", "json"]
    if revision is not None:
        command += ["--revision", str(revision)]
    return run_json(command) or {}


def deployments_named(items: list[dict[str, Any]], app_name: str) -> list[dict[str, Any]]:
    return [item for item in items if (item["metadata"].get("labels") or {}).get(APP_NAME_LABEL) == app_name]


def release_deployments(
    release: str, namespace: str, values: dict[str, Any]
) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    """Return the release's (orchestrator, watcher) Deployments — its two database writers.

    Matched on the chart's selector labels rather than by rebuilding the fullname template,
    so ``fullnameOverride`` needs no handling. The instance label alone is not enough: every
    enabled subchart shares it. The watcher is optional (``watcher.enabled``).
    """
    app_name = values.get("nameOverride") or CHART_NAME
    listing = run_json(
        [
            "kubectl",
            "get",
            "deployments",
            "--namespace",
            namespace,
            "--selector",
            f"app.kubernetes.io/instance={release}",
            "--output",
            "json",
        ]
    )
    deployments = listing.get("items", [])
    orchestrators = deployments_named(deployments, app_name)
    watchers = deployments_named(deployments, f"{app_name}{WATCHER_NAME_SUFFIX}")

    if len(orchestrators) != 1:
        found = ", ".join(sorted(item["metadata"]["name"] for item in deployments)) or "none"
        raise RollbackError(
            f"Expected exactly one Deployment labelled {APP_NAME_LABEL}={app_name} in release {release} "
            f"({namespace}); the release's Deployments are: {found}"
        )
    return orchestrators[0], watchers[0] if watchers else None


def migration_job_name(deployment_name: str, suffix: str) -> str:
    """Fit ``<deployment>-<suffix>`` into the Job name limit by trimming the deployment part.

    ``idegym.fullname`` is itself truncated to 63 characters, so a long release name would
    otherwise fail the rollback on its own Job rather than on anything about the schema.
    """
    keep = max(1, MAX_JOB_NAME_LENGTH - len(suffix) - 1)
    return f"{deployment_name[:keep].rstrip('-')}-{suffix}"


def orchestrator_container(deployment: dict[str, Any]) -> dict[str, Any]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    for container in containers:
        if container["name"] == ORCHESTRATOR_CONTAINER:
            return container
    raise RollbackError(f"Deployment {deployment['metadata']['name']} has no {ORCHESTRATOR_CONTAINER} container")


def build_migration_job(
    name: str,
    namespace: str,
    deployment: dict[str, Any],
    target_revision: str,
    deadline_seconds: int,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """A one-shot Job that migrates the schema using the currently deployed image.

    Copying the image and environment from the live orchestrator is the point: the target
    release's image predates the migrations being reverted and cannot run them.
    ``backoffLimit: 0`` stops a failed downgrade being retried against a half-reverted
    schema. ``dry_run`` resolves and prints the plan without applying it.
    """
    container = orchestrator_container(deployment)
    arguments = [*MIGRATION_CLI, "migrate", "--target", target_revision, "--allow-downgrade"]
    if dry_run:
        arguments.append("--dry-run")
    pod_spec: dict[str, Any] = {
        key: value for key, value in deployment["spec"]["template"]["spec"].items() if key in INHERITED_POD_SPEC_KEYS
    }
    pod_spec |= {
        "restartPolicy": "Never",
        "containers": [
            {
                "name": "migrate",
                "image": container["image"],
                "imagePullPolicy": container.get("imagePullPolicy", "IfNotPresent"),
                "env": container.get("env", []),
                "command": arguments,
            }
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline_seconds,
            "template": {"spec": pod_spec},
        },
    }


def create_job(job: dict[str, Any]) -> None:
    name = job["metadata"]["name"]
    print(f"$ kubectl create -f - # Job/{name}")
    result = subprocess.run(
        ["kubectl", "create", "--namespace", job["metadata"]["namespace"], "--filename", "-"],
        input=json.dumps(job),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        if "already exists" in detail:
            raise RollbackError(
                f"Job {name} already exists — another rollback may be running. Inspect it "
                f"(`kubectl logs job/{name} -n {job['metadata']['namespace']}`), then delete it to retry"
            )
        raise RollbackError(f"Could not create Job {name}: {detail}")


def delete_job(name: str, namespace: str) -> None:
    subprocess.run(
        ["kubectl", "delete", "job", name, "--namespace", namespace, "--ignore-not-found"],
        capture_output=True,
        check=False,
    )


def wait_for_job(name: str, namespace: str, timeout_seconds: int) -> bool:
    """Poll until the Job succeeds or fails, then print its logs. True when it succeeded."""
    deadline = time.monotonic() + timeout_seconds
    succeeded = False
    while True:
        status = run_json(["kubectl", "get", "job", name, "--namespace", namespace, "--output", "json"]).get(
            "status", {}
        )
        if status.get("succeeded"):
            succeeded = True
            break
        if status.get("failed"):
            break
        if time.monotonic() >= deadline:
            print(f"Job {name} did not finish within {timeout_seconds}s", file=sys.stderr)
            break
        time.sleep(JOB_POLL_INTERVAL_SECONDS)

    logs = subprocess.run(
        ["kubectl", "logs", f"job/{name}", "--namespace", namespace, "--tail=-1"],
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"--- Job/{name} logs ---\n{logs.stdout}{logs.stderr}--- end of logs ---")
    return succeeded


def exec_in_deployment(name: str, namespace: str, command: list[str]) -> tuple[int, str, str]:
    """Run a command in one of the Deployment's pods; returns exit code, stdout, stderr.

    Kept apart because the container logs to stderr, so a caller reading a value wants
    stdout alone.
    """
    full = ["kubectl", "exec", f"deployment/{name}", "--namespace", namespace, "--", *command]
    print(f"$ {' '.join(full)}")
    result = subprocess.run(full, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def scale(name: str, namespace: str, replicas: int) -> None:
    run(["kubectl", "scale", "deployment", name, "--namespace", namespace, f"--replicas={replicas}"])


def wait_for_no_pods(deployment: dict[str, Any], namespace: str, timeout_seconds: int) -> None:
    """Block until the Deployment reports no running replicas.

    Scaling to zero returns immediately, but the downgrade must not start while a writer is
    still finishing a request against the schema it is about to lose.
    """
    name = deployment["metadata"]["name"]
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = run_json(["kubectl", "get", "deployment", name, "--namespace", namespace, "--output", "json"]).get(
            "status", {}
        )
        if not status.get("replicas"):
            return
        if time.monotonic() >= deadline:
            raise RollbackError(f"Deployment {name} still has {status.get('replicas')} pods after {timeout_seconds}s")
        time.sleep(JOB_POLL_INTERVAL_SECONDS)


def replica_count(deployment: dict[str, Any]) -> int:
    return int(deployment["spec"].get("replicas", 1))


def preflight_downgrade(deployment: dict[str, Any], namespace: str, target_revision: str, timeout_seconds: int) -> None:
    """Ask the source image whether it can reach ``target_revision``, changing nothing.

    A Job rather than ``kubectl exec``: a rollback usually follows a failed rollout, when no
    pod of that image may be healthy enough to exec into — and a pod of the *previous* image
    would answer wrongly.
    """
    name = migration_job_name(deployment["metadata"]["name"], f"migrate-check-{target_revision}")
    create_job(build_migration_job(name, namespace, deployment, target_revision, timeout_seconds, dry_run=True))
    try:
        if not wait_for_job(name, namespace, timeout_seconds):
            raise RollbackError(
                f"The deployed image cannot migrate to {target_revision} (see the Job logs above). "
                "Nothing was stopped and the release is untouched"
            )
    finally:
        delete_job(name, namespace)


def _parse_args(argv: Optional[list[str]]) -> Namespace:
    parser = ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--release", required=True, help="Helm release name")
    parser.add_argument("--namespace", required=True, help="Namespace the release is installed in")
    parser.add_argument(
        "--revision", required=True, type=int, help="Exact Helm revision to roll back to (see `helm history`)"
    )
    parser.add_argument(
        "--target-schema-revision",
        help="Alembic revision the target release expects. Only needed when it predates database.schemaRevision",
    )
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to allow each waiting step (default: 600)")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and exit without changing anything")
    parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt shown before a schema downgrade"
    )
    return parser.parse_args(argv)


def confirm(plan: str) -> bool:
    print(f"\n{plan}\n")
    try:
        return input("Type 'yes' to continue: ").strip().lower() == "yes"
    except EOFError:
        # Nothing on stdin to answer with — a pipeline has to opt in with --yes.
        print("No terminal to confirm on; pass --yes to approve the downgrade.", file=sys.stderr)
        return False


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    release, namespace, timeout = args.release, args.namespace, args.timeout

    try:
        return _rollback(args, release, namespace, timeout)
    except RollbackError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _rollback(args: Namespace, release: str, namespace: str, timeout: int) -> int:
    source_revision = current_helm_revision(release, namespace)
    if args.revision == source_revision:
        raise RollbackError(f"Release {release} is already at revision {source_revision}")

    history = run_json([*helm(["history"], release, namespace), "--output", "json"])
    target = find_history_entry(history, args.revision)

    deployed_values = release_values(release, namespace)
    source_schema = declared_schema_revision(deployed_values, role="deployed")
    target_schema = args.target_schema_revision or declared_schema_revision(
        release_values(release, namespace, args.revision)
    )

    orchestrator, watcher = release_deployments(release, namespace, deployed_values)
    orchestrator_name = orchestrator["metadata"]["name"]
    writers = [orchestrator, *([watcher] if watcher else [])]
    downgrade_needed = source_schema != target_schema

    print(
        f"\nRelease {release} in {namespace}\n"
        f"  from revision {source_revision} (schema {source_schema})\n"
        f"  to   revision {args.revision} (schema {target_schema}, chart {target.get('chart')})\n"
        f"  orchestrator image {orchestrator_container(orchestrator)['image']}\n"
        f"  schema downgrade: {'yes' if downgrade_needed else 'not needed'}"
    )

    if downgrade_needed:
        preflight_downgrade(orchestrator, namespace, target_schema, timeout)

    if args.dry_run:
        print("\n--dry-run: the release, the schema, and both writers are untouched.")
        return 0

    if downgrade_needed and not args.yes:
        plan = (
            f"This reverts schema revisions down to {target_schema} and permanently discards the data they hold.\n"
            f"There is no automatic backup. {len(writers)} Deployment(s) will be stopped first."
        )
        if not confirm(plan):
            print("Aborted; nothing was changed.")
            return 1

    if not downgrade_needed:
        run([*helm(["rollback"], release, namespace), str(args.revision), "--wait", "--timeout", f"{timeout}s"])
        _verify(orchestrator_name, namespace, target_schema, writers)
        print(f"\nRolled {release} back to revision {args.revision}; the schema was already at {target_schema}.")
        return 0

    original_replicas = {deployment["metadata"]["name"]: replica_count(deployment) for deployment in writers}
    restore_hint = "\n".join(
        f"  kubectl scale deployment {name} -n {namespace} --replicas={replicas}"
        for name, replicas in original_replicas.items()
    )

    print("\nStopping database writers...")
    for name in original_replicas:
        scale(name, namespace, 0)
    for deployment in writers:
        wait_for_no_pods(deployment, namespace, timeout)

    job_name = migration_job_name(orchestrator_name, f"migrate-{target_schema}")
    create_job(build_migration_job(job_name, namespace, orchestrator, target_schema, timeout))
    if not wait_for_job(job_name, namespace, timeout):
        raise RollbackError(
            f"The downgrade to {target_schema} failed, so the Helm release was left at revision {source_revision} "
            f"and every writer is still stopped — the schema may be partially reverted.\n"
            f"Inspect the Job above, then either fix the cause and re-run, or restore a backup.\n"
            f"To bring the current release back up as it was:\n{restore_hint}"
        )

    # Logs are printed above, and leaving it would collide with the next rollback to this
    # revision. A *failed* Job is kept on purpose.
    delete_job(job_name, namespace)

    print(f"\nSchema is at {target_schema}; handing over to Helm.")
    try:
        run(
            [
                *helm(["rollback"], release, namespace),
                str(args.revision),
                "--wait",
                "--wait-for-jobs",
                "--timeout",
                f"{timeout}s",
            ]
        )
    except RollbackError as e:
        raise RollbackError(
            f"{e}\nThe schema was downgraded to {target_schema} but the release was not rolled back, so the writers "
            f"are still stopped. Retry `helm rollback {release} {args.revision} -n {namespace} --wait`, or start the "
            f"current release again after upgrading the schema back:\n{restore_hint}"
        ) from e

    _verify(orchestrator_name, namespace, target_schema, writers)
    print(f"\nRolled {release} back to revision {args.revision} with the schema at {target_schema}.")
    return 0


def _verify(orchestrator_name: str, namespace: str, expected_schema: str, writers: list[dict[str, Any]]) -> None:
    """Confirm the schema and the writers after Helm reports success.

    ``helm rollback --wait`` already gates on the ``/health`` readiness probe, so this adds
    only what it cannot see: the recorded revision, and that every writer came back.
    """
    code, stdout, stderr = exec_in_deployment(
        orchestrator_name, namespace, [*MIGRATION_CLI, "schema", "verify", "--expect", expected_schema]
    )
    if code != 0:
        raise RollbackError(f"The rolled-back release is not at schema revision {expected_schema}: {stderr or stdout}")

    for deployment in writers:
        name = deployment["metadata"]["name"]
        status = run_json(["kubectl", "get", "deployment", name, "--namespace", namespace, "--output", "json"])
        ready = status.get("status", {}).get("readyReplicas") or 0
        expected = replica_count(status)
        if ready != expected:
            raise RollbackError(f"Deployment {name} has {ready}/{expected} ready replicas after the rollback")


if __name__ == "__main__":
    raise SystemExit(main())
