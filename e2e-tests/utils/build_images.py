import subprocess
import tempfile

from from_root import from_root
from idegym.image.dockerfile import render_server_image_dockerfile
from idegym.utils.logging import get_logger
from python_on_whales import DockerClient

logger = get_logger(__name__)

docker = DockerClient()


def _push_to_registry_from_cluster(source_image_tag: str) -> None:
    """
    Push an image to the local Minikube registry from inside the cluster.

    Creates a Kubernetes job that:
    1. Uses skopeo to copy the image from containerd to the registry
    2. Runs inside the cluster where it can access registry.kube-system.svc.cluster.local

    Args:
        source_image_tag: Full image tag (e.g., ghcr.io/.../image:tag)
    """
    from importlib.resources import files

    import config as e2e_config
    from utils.constants import KUBE_SYSTEM_NAMESPACE, PUSH_LOCAL_REGISTRY_HOST
    from utils.k8s_jobs import run_job_sync

    image_name = source_image_tag.split("/")[-1]  # Extract image:tag
    dest_tag = f"{PUSH_LOCAL_REGISTRY_HOST}/{image_name}"

    logger.info(f"Creating registry push job for {source_image_tag} -> {dest_tag}")

    # Load job template and substitute values
    template_path = files(e2e_config).joinpath("registry-push-job.yaml")
    job_manifest = template_path.read_text(encoding="utf-8").format(
        source_image=source_image_tag,
        dest_image=dest_tag,
    )

    success = run_job_sync(job_manifest, namespace=KUBE_SYSTEM_NAMESPACE, timeout=120)
    if not success:
        raise RuntimeError(f"Failed to push image {source_image_tag} to registry")

    logger.info("✓ Successfully pushed image to local registry")


def build_orchestrator_image() -> None:
    """
    Build the orchestrator image and load it into minikube.
    Uses the existing build_orchestrator_image.py script from the main repo.
    """
    logger.info("Building orchestrator image...")

    script_path = from_root("scripts", "build_orchestrator_image.py")

    if not script_path.exists():
        raise FileNotFoundError(f"Build script not found: {script_path}")

    # The script is executable and has a shebang
    cmd = [str(script_path), "--versions", "latest"]

    logger.info(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=from_root(), check=True)

    logger.info("✓ Orchestrator image built successfully")


def switch_to_default_docker_builder() -> None:
    """
    Switch to the default docker builder to enable access to local images.

    Buildx with container driver doesn't have access to local docker images,
    so we need to switch to the default builder which uses the docker driver.
    """
    logger.info("Switching to default docker context and builder...")

    try:
        docker.context.use("default")
    except Exception as e:
        logger.warning(f"Could not switch context: {e}")

    try:
        docker.buildx.use("default")
        logger.info("✓ Switched to default builder")
    except Exception as e:
        logger.warning(f"Could not switch builder: {e}")


def build_base_server_image() -> str:
    """
    Build the base server image with the correct tag.

    This makes the image available in two places:
    1. Local Docker - so IdeGYMDockerAPI can use it as a base for building test images
    2. Minikube - so pods can execute the test images

    Returns:
        str: The image tag
    """
    logger.info("Building base server image...")

    image_tag = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

    rendered_dockerfile = render_server_image_dockerfile(
        repository="docker.io/library",
        image="debian",
        tag="bookworm-20250520-slim",
    )

    # Write rendered Dockerfile to a temporary file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".Dockerfile") as tmp:
        tmp.write(rendered_dockerfile)
        tmp.flush()

        logger.info(f"Building: {image_tag}")
        for line in docker.build(
            context_path=from_root(),
            file=tmp.name,
            tags=[image_tag],
            progress="plain",
            stream_logs=True,
        ):
            if line := line.strip():
                logger.info(line)

        logger.info("✓ Base server image built and available in local Docker")

    # Switch to default docker builder for local image access
    switch_to_default_docker_builder()

    # Tag for local registry
    registry_tag = f"registry.kube-system.svc.cluster.local/{image_tag.split('/')[-1]}"

    logger.info(f"Tagging image for local registry: {registry_tag}")
    subprocess.run(
        ["docker", "tag", image_tag, registry_tag],
        check=True,
    )

    # Load both tags into minikube
    logger.info("Loading base image into minikube...")
    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
    )
    subprocess.run(
        ["minikube", "image", "load", registry_tag],
        check=True,
    )

    # Push to local registry using a Kubernetes job (from inside cluster)
    logger.info("Pushing base image to local registry...")
    _push_to_registry_from_cluster(image_tag)
    logger.info("✓ Base server image loaded into minikube and pushed to registry")

    return image_tag


def build_all_images() -> None:
    logger.info("Building all required images...")

    switch_to_default_docker_builder()
    build_orchestrator_image()
    build_base_server_image()

    logger.info("✓ All images built successfully")
