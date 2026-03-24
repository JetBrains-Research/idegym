"""Kubernetes and minikube setup utilities for local testing."""

import subprocess
import time
from pathlib import Path

import requests
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

BASE_URL = "http://idegym-local.test"


def get_config_dir() -> Path:
    """Get the config directory containing kustomization.yaml."""
    return Path(__file__).parent.parent / "config"


def ensure_ingress_loadbalancer() -> None:
    """
    Ensure the ingress-nginx controller is using LoadBalancer type.

    This is required for minikube tunnel to assign an external IP.
    """
    logger.info("Configuring ingress controller...")

    cmd = [
        "kubectl",
        "patch",
        "svc",
        "ingress-nginx-controller",
        "-n",
        "ingress-nginx",
        "-p",
        '{"spec":{"type":"LoadBalancer"}}',
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        if "patched" in result.stdout or "no change" in result.stdout:
            logger.info("✓ Ingress controller configured as LoadBalancer")
    else:
        logger.warning(f"Could not configure ingress: {result.stderr}")


def apply_kubernetes_resources() -> None:
    """Apply all Kubernetes resources using kustomize."""
    logger.info("Applying Kubernetes resources...")

    kustomization_dir = get_config_dir()

    cmd = ["kubectl", "apply", "-k", str(kustomization_dir)]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)

    if result.returncode == 0:
        logger.info("✓ Kubernetes resources applied successfully")
    else:
        raise RuntimeError(f"Failed to apply resources: {result.stderr}")


def delete_kubernetes_resources() -> None:
    """Delete all Kubernetes resources using kustomize."""
    logger.info("Deleting Kubernetes resources...")

    kustomization_dir = get_config_dir()

    cmd = ["kubectl", "delete", "-k", str(kustomization_dir)]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)

    if result.returncode == 0:
        logger.info("✓ Kubernetes resources deleted successfully")
    else:
        logger.warning(f"Could not delete all resources: {result.stderr}")


def wait_for_service(timeout: int = 120, check_interval: int = 10) -> bool:
    """
    Wait for the orchestrator service to become responsive.

    Args:
        timeout: Maximum time to wait in seconds
        check_interval: Time between checks in seconds

    Returns:
        bool: True if service is responsive, False if timeout reached
    """
    logger.info(f"Waiting for service at {BASE_URL}/health...")

    start_time = time.time()

    while time.time() - start_time < timeout:
        elapsed = int(time.time() - start_time)

        try:
            response = requests.get(f"{BASE_URL}/health", timeout=5)
            if response.status_code == 200:
                logger.info(f"✓ Service is responsive (elapsed: {elapsed}s)")
                return True
        except requests.exceptions.RequestException:
            pass

        logger.info(f"Service not yet responsive, waiting... (elapsed: {elapsed}s)")
        time.sleep(check_interval)

    logger.error(f"Service did not become responsive within {timeout}s")
    return False


def setup_kubernetes_environment(reuse_resources: bool = False) -> bool:
    """
    Set up the complete Kubernetes environment for testing.

    Args:
        reuse_resources: If True, skip resource creation and reuse existing ones

    Returns:
        bool: True if setup successful, False otherwise
    """
    if not reuse_resources:
        logger.info("Setting up Kubernetes environment...")
        ensure_ingress_loadbalancer()
        apply_kubernetes_resources()
    else:
        logger.info("Reusing existing Kubernetes resources")

    return wait_for_service()


def cleanup_kubernetes_environment() -> None:
    """Clean up the Kubernetes environment."""
    logger.info("Cleaning up Kubernetes environment...")
    delete_kubernetes_resources()
