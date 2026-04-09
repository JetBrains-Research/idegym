import asyncio
import subprocess
from importlib.resources import as_file, files

import config as e2e_config
import pytest
import resources as e2e_resources
import yaml
from idegym.api.docker import BaseImage
from idegym.api.git import GitRepository, GitRepositorySnapshot
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.utils.logging import get_logger
from kubernetes_asyncio import config as k8s_config
from utils import k8s_client
from utils.build_images import build_all_images
from utils.constants import (
    DEFAULT_NAMESPACE,
    KUBE_SYSTEM_NAMESPACE,
    ORCHESTRATOR_APP_LABEL,
    REGISTRY_PULL_JOB_NAME,
    REGISTRY_PUSH_JOB_NAME,
)
from utils.idegym_utils import generate_test_id
from utils.k8s_jobs import delete_job, get_all_server_pod_logs, run_job
from utils.k8s_setup import cleanup_kubernetes_environment, setup_kubernetes_environment, wait_for_service

logger = get_logger(__name__)


def load_test_image_commands() -> str:
    return files(e2e_resources).joinpath("test_image_commands.Dockerfile").read_text(encoding="utf-8")


def load_websocket_test_image_commands() -> str:
    return files(e2e_resources).joinpath("openenv_websocket_test_image_commands.Dockerfile").read_text(encoding="utf-8")


def _test_project_snapshot() -> GitRepositorySnapshot:
    return GitRepositorySnapshot(
        repository=GitRepository.parse("https://github.com/realpython/python-scripts.git"),
        reference="cb448c2dc3593dbfbe1ca47b49193b320115aae5",
    )


@pytest.fixture
def test_id() -> str:
    return generate_test_id()


@pytest.fixture(autouse=True)
async def log_server_pods_on_timeout(request):
    """Catch TimeoutError and log all server pod logs."""
    yield
    if request.node.rep_call.failed and "TimeoutError" in str(request.node.rep_call.longrepr):
        logger.error("Test failed with TimeoutError - collecting server pod logs...")
        logs_dict = await get_all_server_pod_logs(namespace=DEFAULT_NAMESPACE)
        if logs_dict:
            for pod_name, logs in logs_dict.items():
                logger.error(f"Server pod {pod_name} logs:\n{logs}")
        else:
            logger.warning("No server pod logs found")


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Make test result available to fixtures."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


def pytest_addoption(parser):
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


@pytest.fixture(scope="session", autouse=True)
def k8s_config_loader():
    """Load Kubernetes configuration once per test session."""
    asyncio.run(k8s_config.load_kube_config())
    logger.info("✓ Loaded Kubernetes configuration")
    yield


@pytest.fixture(scope="session", autouse=True)
def setup_and_cleanup_environment(request, k8s_config_loader):
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
    selector = k8s_client.resolve_pod_selector(app_label, namespace=namespace)
    return k8s_client.list_pod_names(namespace=namespace, label_selector=selector)


def redeploy_orchestrator():
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


def cleanup_kaniko_jobs():
    """
    Delete all Kaniko-related jobs in kube-system namespace.

    This includes:
    - registry-push-job (pushes base image to registry)
    - registry-pull-job (pulls images from registry to containerd)
    """

    async def _cleanup():
        await delete_job(REGISTRY_PUSH_JOB_NAME, namespace=KUBE_SYSTEM_NAMESPACE)
        await delete_job(REGISTRY_PULL_JOB_NAME, namespace=KUBE_SYSTEM_NAMESPACE)

    try:
        asyncio.run(_cleanup())
        logger.debug("✓ Kaniko jobs cleaned up")
    except Exception as e:
        logger.debug(f"Error during Kaniko job cleanup (may not exist): {e}")


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield
    cleanup_servers()
    cleanup_kaniko_jobs()
    redeploy_orchestrator()


def delete_namespace():
    logger.info(f"Deleting {DEFAULT_NAMESPACE} namespace...")
    if k8s_client.delete_namespace(DEFAULT_NAMESPACE, timeout=120):
        logger.info("✓ Namespace deleted")
    else:
        logger.warning("Namespace deletion timed out")


def _extract_service_names_from_kustomize(kustomize_output: str) -> set[str]:
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


async def pull_image_from_registry_to_containerd(image_tag: str) -> None:
    """
    Pull an image from the local registry into Minikube's containerd.

    This is needed because pods can't directly pull from registry.kube-system.svc.cluster.local
    via standard imagePullPolicy - we need to explicitly import it into containerd.

    Uses a privileged Kubernetes job with hostPath mounts to access the host's containerd socket.

    Args:
        image_tag: Full image tag in the registry (e.g., registry.kube-system.svc.cluster.local/image:tag)
    """
    from importlib.resources import files

    import config as e2e_config
    from utils.constants import KUBE_SYSTEM_NAMESPACE

    logger.info(f"Pulling image from registry into containerd: {image_tag}")

    # Load job template and substitute values
    template_path = files(e2e_config).joinpath("registry-pull-job.yaml")
    job_manifest = template_path.read_text(encoding="utf-8").format(image_tag=image_tag)

    success = await run_job(job_manifest, namespace=KUBE_SYSTEM_NAMESPACE, timeout=120)
    if not success:
        logger.warning("Registry pull job had issues - image might already be available")
    else:
        logger.info("✓ Successfully pulled image from registry to containerd")


@pytest.fixture(scope="session")
def test_image():
    logger.info("Building test image for session")
    docker_api = IdeGYMDockerAPI()

    project = _test_project_snapshot()

    commands = load_test_image_commands()

    image = docker_api.build(project=project, base=BaseImage.DEBIAN, commands=commands)
    image_tag = str(image.repo_tags[0])
    subprocess.run(["minikube", "image", "load", image_tag], check=True, capture_output=True)

    logger.info(f"Test image built and loaded: {image_tag}")
    return image_tag


@pytest.fixture
def kaniko_image_loader():
    """
    Fixture that returns a function to load Kaniko-built images into containerd.

    Use this after building images with Kaniko to make them available for pod deployment.
    """
    return pull_image_from_registry_to_containerd


@pytest.fixture(scope="session")
def websocket_test_image():
    logger.info("Building websocket test image for session")
    docker_api = IdeGYMDockerAPI()

    image = docker_api.build(
        project=_test_project_snapshot(),
        base=BaseImage.DEBIAN,
        commands=load_websocket_test_image_commands(),
    )
    image_tag = str(image.repo_tags[0])
    subprocess.run(["minikube", "image", "load", image_tag], check=True, capture_output=True)

    logger.info(f"Websocket test image built and loaded: {image_tag}")
    return image_tag
