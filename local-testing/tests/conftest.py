"""Pytest configuration and shared fixtures for local testing."""

import subprocess
from pathlib import Path

import pytest
from idegym.api.docker import BaseImage
from idegym.api.git import GitRepositorySnapshot
from idegym.client import IdeGYMDockerAPI
from idegym.utils.logging import get_logger
from scripts.k8s_setup import wait_for_service

logger = get_logger(__name__)

# Path to local-testing config directory
CONFIG_DIR = Path(__file__).parent.parent / "config"


def pytest_addoption(parser):
    """Add custom command-line options for cleanup behavior."""
    parser.addoption(
        "--delete-namespace",
        action="store_true",
        default=False,
        help="Delete the entire idegym-local namespace after all tests complete",
    )
    parser.addoption(
        "--delete-kustomize-services",
        action="store_true",
        default=False,
        help="Delete only services defined in kustomization.yaml after all tests complete",
    )


def cleanup_servers():
    """Delete all server resources (deployments, services, PDBs) in idegym-local namespace."""
    logger.info("Cleaning up server resources after test...")

    # Find all pods with container name=server
    # Using jsonpath to filter pods that have a container named "server"
    list_cmd = [
        "kubectl",
        "get",
        "pods",
        "-n",
        "idegym-local",
        "-o",
        "json",
    ]
    list_result = subprocess.run(list_cmd, check=False, capture_output=True, text=True, timeout=30)

    if list_result.returncode == 0 and list_result.stdout.strip():
        import json

        try:
            pods_data = json.loads(list_result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning(f"Could not parse pod list JSON, skipping server cleanup: {exc}")
            return
        deployment_names = set()

        # Find pods with container named "server"
        for pod in pods_data.get("items", []):
            containers = pod.get("spec", {}).get("containers", [])
            has_server_container = any(c.get("name") == "server" for c in containers)

            if has_server_container:
                pod_name = pod.get("metadata", {}).get("name", "")
                # Extract deployment name from pod name
                # Pod name format: {deployment-name}-{replicaset-hash}-{pod-hash}
                # e.g., lifecycle-4112939b-1-5f6d8c88f6-k2cwl -> lifecycle-4112939b-1
                parts = pod_name.rsplit("-", 2)
                if len(parts) >= 3:
                    deployment_name = parts[0]
                    deployment_names.add(deployment_name)

        # Delete resources for each deployment
        for deployment_name in deployment_names:
            # Delete deployment
            subprocess.run(
                [
                    "kubectl",
                    "delete",
                    "deployment",
                    deployment_name,
                    "-n",
                    "idegym-local",
                    "--ignore-not-found=true",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            # Delete service
            subprocess.run(
                [
                    "kubectl",
                    "delete",
                    "service",
                    f"{deployment_name}-service",
                    "-n",
                    "idegym-local",
                    "--ignore-not-found=true",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            # Delete PDB
            subprocess.run(
                [
                    "kubectl",
                    "delete",
                    "poddisruptionbudget",
                    f"{deployment_name}-pdb",
                    "-n",
                    "idegym-local",
                    "--ignore-not-found=true",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            # Delete ReplicaSets with either common label convention.
            for selector in (f"app={deployment_name}", f"app.kubernetes.io/name={deployment_name}"):
                subprocess.run(
                    [
                        "kubectl",
                        "delete",
                        "replicaset",
                        "-n",
                        "idegym-local",
                        "-l",
                        selector,
                        "--ignore-not-found=true",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

        logger.info(f"✓ Server resources cleaned up ({len(deployment_names)} servers)")
    else:
        logger.info("✓ No server resources to clean up")


def resolve_pod_selector(app_label: str, namespace: str = "idegym-local") -> str:
    """Return a pod selector that matches existing pods, falling back to app.kubernetes.io/name."""
    selectors = [f"app.kubernetes.io/name={app_label}", f"app={app_label}"]

    for selector in selectors:
        result = subprocess.run(
            ["kubectl", "get", "pod", "-l", selector, "-n", namespace, "-o", "name"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return selector

    return selectors[0]


def list_pods_by_label(app_label: str, namespace: str = "idegym-local") -> list[str]:
    """Return pod names for a given app label."""
    selector = resolve_pod_selector(app_label, namespace=namespace)
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "pod",
            "-l",
            selector,
            "-n",
            namespace,
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0 or not result.stdout.strip():
        return []

    return result.stdout.strip().split()


def redeploy_orchestrator():
    """Redeploy orchestrator between tests."""
    logger.info("Redeploying orchestrator...")

    try:
        app_label = "orchestrator"
        pod_names = list_pods_by_label(app_label, namespace="idegym-local")

        if pod_names:
            subprocess.run(
                ["kubectl", "delete", "pod", "-n", "idegym-local", "--ignore-not-found=true", *pod_names],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

        if not wait_for_service(timeout=180, check_interval=10):
            raise RuntimeError("Orchestrator service did not become responsive in time")

        logger.info("✓ Orchestrator redeployed")
    except Exception as e:
        logger.error(f"Failed to redeploy orchestrator: {e}")
        raise


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Automatically cleanup server pods and redeploy orchestrator/database after each test."""
    yield
    cleanup_servers()
    redeploy_orchestrator()


def delete_namespace():
    """Delete the entire idegym-local namespace."""
    logger.info("Deleting idegym-local namespace...")
    subprocess.run(
        ["kubectl", "delete", "namespace", "idegym-local", "--ignore-not-found=true"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    logger.info("✓ Namespace deleted")


def delete_kustomize_services():
    """Delete only services defined in kustomization.yaml."""
    logger.info("Deleting kustomize services...")
    build_result = subprocess.run(
        ["kubectl", "kustomize", str(CONFIG_DIR)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    if build_result.returncode != 0:
        logger.warning(f"Could not render kustomization: {build_result.stderr}")
        return

    kustomize_output = build_result.stdout

    service_names: set[str] = set()
    try:
        import yaml  # type: ignore

        for doc in yaml.safe_load_all(kustomize_output):
            if not isinstance(doc, dict):
                continue
            if doc.get("kind") != "Service":
                continue
            metadata = doc.get("metadata") or {}
            name = metadata.get("name")
            if name:
                service_names.add(name)
    except Exception:
        current_kind = None
        current_name = None
        in_metadata = False
        metadata_indent = 0

        for line in kustomize_output.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if stripped == "---":
                if current_kind == "Service" and current_name:
                    service_names.add(current_name)
                current_kind = None
                current_name = None
                in_metadata = False
                metadata_indent = 0
                continue

            if not line.startswith(" "):
                in_metadata = False
                metadata_indent = 0
                if stripped.startswith("kind:"):
                    current_kind = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("metadata:"):
                    in_metadata = True
                    metadata_indent = len(line) - len(line.lstrip())
                continue

            if in_metadata:
                indent = len(line) - len(line.lstrip())
                if indent <= metadata_indent:
                    in_metadata = False
                    continue
                if stripped.startswith("name:"):
                    current_name = stripped.split(":", 1)[1].strip()

        if current_kind == "Service" and current_name:
            service_names.add(current_name)

    if not service_names:
        logger.info("✓ No kustomize services found to delete")
        return

    delete_cmd = [
        "kubectl",
        "delete",
        "service",
        "-n",
        "idegym-local",
        "--ignore-not-found=true",
        *sorted(service_names),
    ]
    subprocess.run(
        delete_cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    logger.info(f"✓ Kustomize services deleted ({len(service_names)})")


@pytest.fixture(scope="session", autouse=True)
def cleanup_after_session(request):
    """Cleanup after all tests based on command-line flags."""
    yield

    delete_namespace_flag = request.config.getoption("--delete-namespace")
    delete_services_flag = request.config.getoption("--delete-kustomize-services")

    if delete_namespace_flag:
        delete_namespace()
    elif delete_services_flag:
        delete_kustomize_services()


@pytest.fixture(scope="session")
def test_image():
    """Build and cache test image for the entire test session."""
    logger.info("Building test image for session")
    docker_api = IdeGYMDockerAPI()

    project = GitRepositorySnapshot(
        repository={"server": "github.com", "owner": "realpython", "name": "python-scripts"},
        reference="cb448c2dc3593dbfbe1ca47b49193b320115aae5",
    )

    commands = """
USER root
RUN set -eux; \\
    apt-get update; \\
    apt-get install -y --no-install-recommends \\
    python3=3.11.2* \\
    python-is-python3=3.11.2*; \\
    rm -rf /var/lib/apt/lists/*
USER appuser
"""

    image = docker_api.build(project=project, base=BaseImage.DEBIAN, commands=commands)
    image_tag = str(image.repo_tags[0])
    subprocess.run(["minikube", "image", "load", image_tag], check=True, capture_output=True)

    logger.info(f"Test image built and loaded: {image_tag}")
    return image_tag
