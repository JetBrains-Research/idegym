"""End-to-end test for the standalone watcher service.

Deploys as part of the Helm chart (see ``e2e-tests/config/values.yaml``), then verifies the
watcher — running in its own deployment, separate from the orchestrator — actually performs
cleanup in-cluster: a stale server row is transitioned to ``KILLED``.
"""

import time

import pytest
from idegym.utils.logging import get_logger
from utils import k8s_client
from utils.constants import DEFAULT_NAMESPACE, POSTGRESQL_APP_LABEL, POSTGRESQL_DB, POSTGRESQL_USER, WATCHER_APP_LABEL
from utils.k8s_setup import wait_for_pod_ready

logger = get_logger(__name__)

_PG_SUPERUSER_PASSWORD = "postgres"


def _exec_psql(sql: str) -> str:
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
    )


@pytest.mark.e2e
def test_watcher_cleans_up_stale_server(test_id):
    # The watcher runs in its own deployment; helm --wait already gates on it, but assert explicitly.
    assert wait_for_pod_ready(WATCHER_APP_LABEL, timeout=120), "Watcher pod did not become ready"

    generated_name = f"watcher-e2e-{test_id}"
    now_ms = int(time.time() * 1000)

    # Insert a stale client + server (last_heartbeat_time=0 => always past the inactivity timeout).
    # No backing Kubernetes deployment exists, so the watcher's clean_up_server is a no-op success
    # and the server is transitioned to KILLED.
    insert_sql = (
        "WITH c AS ("
        "  INSERT INTO clients (id, name, namespace, created_at, last_heartbeat_time, availability, nodes_count)"
        f"  VALUES (gen_random_uuid(), 'watcher-e2e', '{DEFAULT_NAMESPACE}', {now_ms}, 0, 'ALIVE', 0)"
        "  RETURNING id"
        ") "
        "INSERT INTO servers (client_id, client_name, server_name, generated_name, namespace, created_at,"
        " last_heartbeat_time, availability, cpu, ram, run_as_root, server_kind, service_port) "
        f"SELECT id, 'watcher-e2e', 'srv', '{generated_name}', '{DEFAULT_NAMESPACE}', {now_ms}, 0, 'ALIVE',"
        " 0, 0, false, 'idegym', 80 FROM c;"
    )
    _exec_psql(insert_sql)
    logger.info(f"Inserted stale server {generated_name}")

    deadline = time.time() + 90
    status = ""
    while time.time() < deadline:
        status = _exec_psql(f"SELECT availability FROM servers WHERE generated_name = '{generated_name}';").strip()
        logger.info(f"Server {generated_name} availability: {status!r}")
        if status == "KILLED":
            return
        time.sleep(3)

    pytest.fail(f"Watcher did not transition stale server to KILLED in time (last status: {status!r})")


@pytest.mark.e2e
def test_watcher_leaves_a_server_held_by_keepalive(test_id):
    """A stale row that would be reaped survives while its keepalive window is open.

    Uses the same always-stale row as the test above, so the only difference between the two
    outcomes is `keepalive_until`.
    """
    assert wait_for_pod_ready(WATCHER_APP_LABEL, timeout=120), "Watcher pod did not become ready"

    generated_name = f"watcher-keepalive-{test_id}"
    now_ms = int(time.time() * 1000)
    held_until_ms = now_ms + 30 * 60 * 1000

    insert_sql = (
        "WITH c AS ("
        "  INSERT INTO clients (id, name, namespace, created_at, last_heartbeat_time, availability, nodes_count)"
        f"  VALUES (gen_random_uuid(), 'watcher-keepalive', '{DEFAULT_NAMESPACE}', {now_ms}, {now_ms}, 'ALIVE', 0)"
        "  RETURNING id"
        ") "
        "INSERT INTO servers (client_id, client_name, server_name, generated_name, namespace, created_at,"
        " last_heartbeat_time, keepalive_until, availability, cpu, ram, run_as_root, server_kind, service_port) "
        f"SELECT id, 'watcher-keepalive', 'srv', '{generated_name}', '{DEFAULT_NAMESPACE}', {now_ms}, 0,"
        f" {held_until_ms}, 'ALIVE', 0, 0, false, 'idegym', 80 FROM c;"
    )
    _exec_psql(insert_sql)
    logger.info(f"Inserted held server {generated_name}")

    # Long enough for several cleanup passes; the unheld test above proves one lands well inside this.
    deadline = time.time() + 90
    while time.time() < deadline:
        status = _exec_psql(f"SELECT availability FROM servers WHERE generated_name = '{generated_name}';").strip()
        assert status == "ALIVE", f"Watcher reaped a server held by keepalive (status: {status!r})"
        time.sleep(5)
