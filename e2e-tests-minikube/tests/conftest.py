"""Pytest configuration and shared fixtures for e2e testing."""

import subprocess
from importlib.resources import as_file, files

import config as e2e_config
import pytest
import yaml
from idegym.api.docker import BaseImage
from idegym.api.git import GitRepositorySnapshot
from idegym.client import IdeGYMDockerAPI
from idegym.utils.logging import get_logger
from utils import k8s_client
from utils.build_images import build_all_images
from utils.constants import DEFAULT_NAMESPACE, ORCHESTRATOR_APP_LABEL
from utils.idegym_utils import generate_test_id
from utils.k8s_setup import cleanup_kubernetes_environment, setup_kubernetes_environment, wait_for_service

logger = get_logger(__name__)

TEST_IMAGE_COMMANDS_PATH = "test_image_commands.Dockerfile"


def load_test_image_commands() -> str:
    """Load the Docker command snippet for the test image from packaged resources."""
    return files(e2e_config).joinpath(TEST_IMAGE_COMMANDS_PATH).read_text(encoding="utf-8")


@pytest.fixture
def test_id() -> str:
    """Return a short unique ID for test resource names."""
    return generate_test_id()


def pytest_addoption(parser):
    """Add custom command-line options for e2e setup and cleanup behavior."""
    parser.addoption(
        "--skip-build",
        action="store_true",
        default=False,
        help="Skip building Docker images before e2e tests",
    )
    parser.addoption(
        "--reuse-resources",
        action="store_true",
        default=False,
        help="Reuse existing Kubernetes resources instead of recreating them",
    )
    parser.addoption(
        "--clean-namespace",
        action="store_true",
        default=False,
        help="Recreate idegym-local namespace before e2e tests",
    )
    parser.addoption(
        "--no-cleanup",
        action="store_true",
        default=False,
        help="Do not delete resources after e2e tests",
    )
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


def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: marks end-to-end test suite")


def pytest_collection_modifyitems(config, items):
    del config

    for item in items:
        if item.nodeid.startswith("tests/") or item.nodeid.startswith("e2e-tests-minikube/tests/"):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session", autouse=True)
def setup_and_cleanup_environment(request):
    """Set up e2e environment before session and clean it up afterwards."""
    skip_build = request.config.getoption("--skip-build")
    reuse_resources = request.config.getoption("--reuse-resources")
    clean_namespace = request.config.getoption("--clean-namespace")

    logger.info("=" * 80)
    logger.info("E2E SESSION SETUP")
    logger.info("=" * 80)

    try:
        if not skip_build:
            logger.info("Building Docker images for e2e tests...")
            build_all_images()
        else:
            logger.info("Skipping Docker image build")

        logger.info("Setting up Kubernetes environment for e2e tests...")
        if not setup_kubernetes_environment(reuse_resources=reuse_resources, clean_namespace=clean_namespace):
            pytest.exit("Failed to set up Kubernetes environment", returncode=1)

        yield

    finally:
        delete_namespace_flag = request.config.getoption("--delete-namespace")
        delete_services_flag = request.config.getoption("--delete-kustomize-services")
        no_cleanup = request.config.getoption("--no-cleanup")

        if delete_namespace_flag:
            delete_namespace()
        elif delete_services_flag:
            delete_kustomize_services()
        elif no_cleanup:
            logger.info("Skipping post-test cleanup due to --no-cleanup")
        else:
            try:
                cleanup_kubernetes_environment(clean_namespace=False)
            except Exception as cleanup_error:
                logger.error(f"Error during cleanup: {cleanup_error}", exc_info=True)


def cleanup_servers():
    """Delete server deployments in the test namespace."""
    logger.info("Cleaning up server deployments after test...")

    label_selector = "app.kubernetes.io/component=sandbox"
    deployment_names = k8s_client.list_deployment_names(namespace=DEFAULT_NAMESPACE, label_selector=label_selector)

    if not deployment_names:
        logger.info("✓ No server deployments to clean up")
        return

    for deployment_name in deployment_names:
        k8s_client.delete_deployment(namespace=DEFAULT_NAMESPACE, deployment_name=deployment_name)

    logger.info(f"✓ Server deployments cleaned up ({len(deployment_names)} servers)")


def list_pods_by_label(app_label: str, namespace: str = DEFAULT_NAMESPACE) -> list[str]:
    """Return pod names for a given app label."""
    selector = k8s_client.resolve_pod_selector(app_label, namespace=namespace)
    return k8s_client.list_pod_names(namespace=namespace, label_selector=selector)


def redeploy_orchestrator():
    """Redeploy orchestrator between tests."""
    logger.info("Redeploying orchestrator...")

    try:
        pod_names = list_pods_by_label(ORCHESTRATOR_APP_LABEL, namespace=DEFAULT_NAMESPACE)

        if pod_names:
            k8s_client.delete_pods(namespace=DEFAULT_NAMESPACE, pod_names=pod_names)
            if not k8s_client.wait_for_pods_deleted(DEFAULT_NAMESPACE, pod_names, timeout=120, check_interval=2):
                raise RuntimeError("Timed out waiting for orchestrator pods to terminate")

        if not wait_for_service(timeout=180, check_interval=10):
            raise RuntimeError("Orchestrator service did not become responsive in time")

        logger.info("✓ Orchestrator redeployed")
    except Exception as e:
        logger.error(f"Failed to redeploy orchestrator: {e}")
        raise


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Automatically cleanup server deployments and redeploy orchestrator/database after each test."""
    yield
    cleanup_servers()
    redeploy_orchestrator()


def delete_namespace():
    """Delete the entire test namespace."""
    logger.info(f"Deleting {DEFAULT_NAMESPACE} namespace...")
    if k8s_client.delete_namespace(DEFAULT_NAMESPACE, timeout=120):
        logger.info("✓ Namespace deleted")
    else:
        logger.warning("Namespace deletion timed out")


def _extract_service_names_from_kustomize(kustomize_output: str) -> set[str]:
    """Extract service names from kustomize output YAML."""
    service_names: set[str] = set()
    for doc in yaml.safe_load_all(kustomize_output):
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") != "Service":
            continue
        metadata = doc.get("metadata") or {}
        name = metadata.get("name")
        if name:
            service_names.add(name)
    return service_names


def delete_kustomize_services():
    """Delete only services defined in kustomization.yaml."""
    logger.info("Deleting kustomize services...")
    with as_file(files(e2e_config)) as config_dir:
        build_result = subprocess.run(
            ["kubectl", "kustomize", str(config_dir)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    if build_result.returncode != 0:
        logger.warning(f"Could not render kustomization: {build_result.stderr}")
        return

    try:
        service_names = _extract_service_names_from_kustomize(build_result.stdout)
    except yaml.YAMLError as e:
        logger.warning(f"Could not parse kustomize output: {e}")
        return

    if not service_names:
        logger.info("✓ No kustomize services found to delete")
        return

    k8s_client.delete_services(namespace=DEFAULT_NAMESPACE, service_names=sorted(service_names))
    logger.info(f"✓ Kustomize services deleted ({len(service_names)})")


@pytest.fixture(scope="session")
def test_image():
    """Build and cache test image for the entire test session."""
    logger.info("Building test image for session")
    docker_api = IdeGYMDockerAPI()

    project = GitRepositorySnapshot(
        repository={"server": "github.com", "owner": "realpython", "name": "python-scripts"},
        reference="cb448c2dc3593dbfbe1ca47b49193b320115aae5",
    )

    commands = load_test_image_commands()

    image = docker_api.build(project=project, base=BaseImage.DEBIAN, commands=commands)
    image_tag = str(image.repo_tags[0])
    subprocess.run(["minikube", "image", "load", image_tag], check=True, capture_output=True)

    logger.info(f"Test image built and loaded: {image_tag}")
    return image_tag
