"""Pytest configuration and shared fixtures for local testing."""

import subprocess

import pytest
from idegym.api.docker import BaseImage
from idegym.api.git import GitRepositorySnapshot
from idegym.client import IdeGYMDockerAPI
from idegym.utils.logging import get_logger

logger = get_logger(__name__)


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

        pods_data = json.loads(list_result.stdout)
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

            # Delete ReplicaSet (using label selector to match all ReplicaSets for this deployment)
            subprocess.run(
                [
                    "kubectl",
                    "delete",
                    "replicaset",
                    "-n",
                    "idegym-local",
                    "-l",
                    f"app={deployment_name}",
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


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Automatically cleanup server pods after each test."""
    yield
    cleanup_servers()


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
