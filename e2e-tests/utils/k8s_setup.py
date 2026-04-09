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
    DEFAULT_HEALTH_CHECK_TIMEOUT,
    DEFAULT_NAMESPACE,
    HEALTH_CHECK_INTERVAL,
    INGRESS_CONTROLLER_SERVICE,
    INGRESS_NAMESPACE,
)

logger = get_logger(__name__)


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


def apply_kubernetes_resources() -> None:
    logger.info("Applying Kubernetes resources...")

    with get_config_dir() as kustomization_dir:
        cmd = ["kubectl", "apply", "-k", str(kustomization_dir)]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    logger.info("✓ Kubernetes resources applied successfully")


def delete_kubernetes_resources() -> None:
    logger.info("Deleting Kubernetes resources from kustomization...")

    with get_config_dir() as kustomization_dir:
        cmd = ["kubectl", "delete", "-k", str(kustomization_dir), "--ignore-not-found=true"]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)

    if result.returncode == 0:
        logger.info("✓ Kubernetes resources deleted successfully")
    else:
        # Only warn if there are actual errors (not just NotFound)
        if result.stderr and "NotFound" not in result.stderr:
            logger.warning(f"Could not delete all resources: {result.stderr}")


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
