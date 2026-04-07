"""Image building utilities for e2e testing."""

import subprocess
import tempfile

from from_root import from_root
from idegym.image.dockerfile import render_server_image_dockerfile
from idegym.utils.logging import get_logger
from python_on_whales import DockerClient

logger = get_logger(__name__)

docker = DockerClient()


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

    # Load into minikube
    logger.info("Loading base image into minikube...")
    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
    )

    logger.info("✓ Base server image loaded into minikube")

    return image_tag


def build_all_images() -> None:
    """Build all required images for e2e testing."""
    logger.info("Building all required images...")

    build_orchestrator_image()
    build_base_server_image()

    logger.info("✓ All images built successfully")
