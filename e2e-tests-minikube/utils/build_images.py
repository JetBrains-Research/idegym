"""Image building utilities for e2e testing."""

import subprocess
import tempfile

from from_root import from_root
from idegym.utils.logging import get_logger
from jinja2 import Template

logger = get_logger(__name__)


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

    # First switch context
    context_result = subprocess.run(
        ["docker", "context", "use", "default"],
        capture_output=True,
        text=True,
    )

    if context_result.returncode != 0:
        logger.warning(f"Could not switch context: {context_result.stderr}")

    # Then switch builder
    builder_result = subprocess.run(
        ["docker", "buildx", "use", "default"],
        capture_output=True,
        text=True,
    )

    if builder_result.returncode == 0:
        logger.info("✓ Switched to default builder")
    else:
        logger.warning(f"Could not switch builder: {builder_result.stderr}")


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

    # Read and render the Dockerfile template
    dockerfile_template_path = from_root("Dockerfile.jinja")
    with open(dockerfile_template_path, "r") as f:
        template = Template(f.read())

    rendered_dockerfile = template.render(repository="docker.io/library", image="debian", tag="bookworm-20250520-slim")

    # Write rendered Dockerfile to a temporary file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".Dockerfile") as tmp:
        tmp.write(rendered_dockerfile)
        tmp.flush()

        # Build the base server image
        cmd = ["docker", "build", "-t", image_tag, "-f", tmp.name, "."]

        logger.info(f"Building: {image_tag}")
        subprocess.run(cmd, cwd=from_root(), check=True)

        logger.info("✓ Base server image built and available in local Docker")

        # Switch to default docker builder for local image access
        switch_to_default_docker_builder()

        # Load into minikube
        logger.info("Loading base image into minikube...")
        subprocess.run(
            ["minikube", "image", "load", image_tag],
            capture_output=True,
            text=True,
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
