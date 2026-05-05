import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from pathlib import Path

import config as e2e_config
import requests
from idegym.utils.logging import get_logger
from utils import k8s_client
from utils.constants import (
    BASE_URL,
    CHART_PATH,
    DEFAULT_HEALTH_CHECK_TIMEOUT,
    DEFAULT_NAMESPACE,
    HEALTH_CHECK_INTERVAL,
    HELM_RELEASE,
    INGRESS_CONTROLLER_SERVICE,
    INGRESS_NAMESPACE,
    POSTGRESQL_APP_LABEL,
    POSTGRESQL_DB,
    POSTGRESQL_USER,
)

logger = get_logger(__name__)

POSTGRES_SECRET_NAME = "postgres"
POSTGRES_APP_USER = "idegym"
POSTGRES_APP_PASSWORD = "idegym"
POSTGRES_SUPERUSER_PASSWORD = "postgres"

GRAFANA_SECRET_NAME = "grafana"
GRAFANA_ADMIN_USER = "admin"
GRAFANA_ADMIN_PASSWORD = "changeme"


@contextmanager
def get_config_dir() -> Iterator[Path]:
    """Yield a filesystem path to the config resource directory."""
    with as_file(files(e2e_config)) as config_dir:
        yield Path(config_dir)


def ensure_ingress_loadbalancer() -> None:
    """
    Ensure the ingress-nginx controller is using LoadBalancer type.

    This is required for minikube tunnel to assign an external IP.
    """
    logger.info("Configuring ingress controller...")

    try:
        if k8s_client.patch_service_type(
            name=INGRESS_CONTROLLER_SERVICE,
            namespace=INGRESS_NAMESPACE,
            service_type="LoadBalancer",
        ):
            logger.info("✓ Ingress controller configured as LoadBalancer")
        else:
            logger.warning(f"Could not configure ingress: service {INGRESS_CONTROLLER_SERVICE} not found")
    except Exception as exc:
        logger.warning(f"Could not configure ingress: {exc}")


def provision_secrets() -> None:
    """
    Create the Secrets the chart and its subcharts consume.

    - `postgres`: chart's deployment.yaml (host/port/database/username/password)
      and postgresql subchart (password/postgres-password).
    - `grafana`: grafana subchart admin credentials (username/password).
    """
    logger.info(f"Provisioning {POSTGRES_SECRET_NAME} secret in {DEFAULT_NAMESPACE}...")
    k8s_client.upsert_secret(
        namespace=DEFAULT_NAMESPACE,
        name=POSTGRES_SECRET_NAME,
        string_data={
            "host": "postgres",
            "port": "5432",
            "database": POSTGRESQL_DB,
            "username": POSTGRES_APP_USER,
            "password": POSTGRES_APP_PASSWORD,
            "postgres-password": POSTGRES_SUPERUSER_PASSWORD,
        },
    )
    logger.info(f"✓ {POSTGRES_SECRET_NAME} secret ready")

    logger.info(f"Provisioning {GRAFANA_SECRET_NAME} secret in {DEFAULT_NAMESPACE}...")
    k8s_client.upsert_secret(
        namespace=DEFAULT_NAMESPACE,
        name=GRAFANA_SECRET_NAME,
        string_data={
            "username": GRAFANA_ADMIN_USER,
            "password": GRAFANA_ADMIN_PASSWORD,
        },
    )
    logger.info(f"✓ {GRAFANA_SECRET_NAME} secret ready")


def apply_kubernetes_resources() -> None:
    logger.info("Installing Helm release...")
    provision_secrets()

    with get_config_dir() as cfg:
        cmd = [
            "helm",
            "upgrade",
            "--install",
            HELM_RELEASE,
            str(CHART_PATH),
            "-n",
            DEFAULT_NAMESPACE,
            "-f",
            str(cfg / "values.yaml"),
            "--wait",
            "--timeout",
            "5m",
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    logger.info("✓ Helm release installed successfully")


def delete_kubernetes_resources() -> None:
    logger.info(f"Uninstalling Helm release {HELM_RELEASE}...")
    cmd = ["helm", "uninstall", HELM_RELEASE, "-n", DEFAULT_NAMESPACE, "--ignore-not-found"]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)

    if result.returncode == 0:
        logger.info("✓ Helm release uninstalled successfully")
    elif result.stderr and "not found" not in result.stderr.lower():
        logger.warning(f"Could not uninstall release: {result.stderr}")


def ensure_namespace_exists() -> None:
    try:
        existed_before = k8s_client.namespace_exists(DEFAULT_NAMESPACE)
        k8s_client.ensure_namespace_exists(DEFAULT_NAMESPACE)
        if existed_before:
            logger.info(f"✓ Namespace {DEFAULT_NAMESPACE} already exists")
        else:
            logger.info(f"✓ Created {DEFAULT_NAMESPACE} namespace")
    except Exception as exc:
        logger.warning(f"Could not ensure namespace exists: {exc}")


def recreate_namespace() -> None:
    logger.info(f"Recreating {DEFAULT_NAMESPACE} namespace...")

    namespace = DEFAULT_NAMESPACE

    try:
        deleted = k8s_client.delete_namespace(namespace, timeout=180)
    except Exception as exc:
        logger.warning(f"Could not delete namespace: {exc}")
        return

    if not deleted:
        logger.warning(f"Skipping namespace creation because {namespace} is still deleting")
        return

    try:
        k8s_client.ensure_namespace_exists(namespace)
        logger.info("✓ Namespace recreated successfully")
    except Exception as exc:
        logger.warning(f"Could not create namespace: {exc}")


def delete_namespace(namespace: str = "idegym-local") -> None:
    logger.info(f"Deleting namespace {namespace}...")
    try:
        if k8s_client.delete_namespace(namespace, timeout=180):
            logger.info(f"✓ Namespace {namespace} deleted successfully")
        else:
            logger.warning(f"Could not delete namespace {namespace}: timeout reached")
    except Exception as exc:
        logger.warning(f"Could not delete namespace {namespace}: {exc}")


def wait_for_service(timeout: int = DEFAULT_HEALTH_CHECK_TIMEOUT, check_interval: int = HEALTH_CHECK_INTERVAL) -> bool:
    logger.info(f"Waiting for service at {BASE_URL}/health...")

    start_time = time.time()
    consecutive_successes = 0
    required_successes = 3

    while time.time() - start_time < timeout:
        elapsed = int(time.time() - start_time)

        try:
            response = requests.get(f"{BASE_URL}/health", timeout=5)
            if response.status_code == 200:
                consecutive_successes += 1
                if consecutive_successes >= required_successes:
                    logger.info(f"✓ Service is responsive (elapsed: {elapsed}s)")
                    return True
                time.sleep(1)
                continue
        except requests.exceptions.RequestException:
            pass

        consecutive_successes = 0
        logger.info(f"Service not yet responsive, waiting... (elapsed: {elapsed}s)")
        time.sleep(check_interval)

    logger.error(f"Service did not become responsive within {timeout}s")
    return False


_DB_RESET_SQL = (
    "TRUNCATE async_operations, job_statuses, servers, clients RESTART IDENTITY CASCADE; "
    "DELETE FROM resource_limit_rules WHERE client_name_regex != '.*'; "
    "UPDATE resource_limit_rules SET used_cpu = 0, used_ram = 0, current_pods = 0 WHERE client_name_regex = '.*';"
)


def reset_orchestrator_db() -> None:
    logger.info("Resetting orchestrator database...")
    pod_names = k8s_client.list_pod_names(
        namespace=DEFAULT_NAMESPACE, label_selector=f"app.kubernetes.io/name={POSTGRESQL_APP_LABEL}"
    )
    if not pod_names:
        raise RuntimeError(f"No postgresql pod found in namespace {DEFAULT_NAMESPACE}")
    output = k8s_client.exec_in_pod(
        pod_name=pod_names[0],
        namespace=DEFAULT_NAMESPACE,
        command=[
            "env",
            f"PGPASSWORD={POSTGRES_SUPERUSER_PASSWORD}",
            "psql",
            "-U",
            POSTGRESQL_USER,
            "-d",
            POSTGRESQL_DB,
            "-c",
            _DB_RESET_SQL,
        ],
    )
    logger.info(f"✓ Database reset complete: {output.strip()}")


def setup_kubernetes_environment(reuse_resources: bool = False, clean_namespace: bool = False) -> bool:
    if clean_namespace:
        recreate_namespace()
    else:
        ensure_namespace_exists()

    if not reuse_resources:
        logger.info("Setting up Kubernetes environment...")
        ensure_ingress_loadbalancer()
        apply_kubernetes_resources()
    else:
        logger.info("Reusing existing Kubernetes resources")

    return wait_for_service()


def wait_for_pod_deleted(
    app_label: str,
    namespace: str = DEFAULT_NAMESPACE,
    timeout: int = 60,
    label_key: str = "app.kubernetes.io/name",
) -> bool:
    logger.info(f"Waiting for {app_label} pod to be deleted...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        elapsed = int(time.time() - start_time)

        # Check if any pods exist
        selector = k8s_client.resolve_pod_selector(app_label, namespace, label_key)
        if not k8s_client.list_pod_names(namespace=namespace, label_selector=selector):
            logger.info(f"✓ {app_label} pod deleted (elapsed: {elapsed}s)")
            return True

        time.sleep(1)

    logger.error(f"{app_label} pod did not delete within {timeout}s")
    return False


def wait_for_pod_ready(
    app_label: str,
    namespace: str = DEFAULT_NAMESPACE,
    timeout: int = 120,
    check_interval: int = 2,
    label_key: str = "app.kubernetes.io/name",
) -> bool:
    logger.info(f"Waiting for {app_label} pod to be ready...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        elapsed = int(time.time() - start_time)

        # Check if pod exists and is ready
        selector = k8s_client.resolve_pod_selector(app_label, namespace, label_key)
        if k8s_client.is_any_pod_ready(namespace=namespace, label_selector=selector):
            logger.info(f"✓ {app_label} pod is ready (elapsed: {elapsed}s)")
            return True

        logger.info(f"{app_label} not yet ready, waiting... (elapsed: {elapsed}s)")
        time.sleep(check_interval)

    logger.error(f"{app_label} did not become ready within {timeout}s")
    return False


def cleanup_kubernetes_environment(clean_namespace: bool = False) -> None:
    logger.info("Cleaning up Kubernetes environment...")
    if clean_namespace:
        delete_namespace()
    else:
        delete_kubernetes_resources()
