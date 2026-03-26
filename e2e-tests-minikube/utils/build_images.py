"""Image building utilities for e2e testing."""

import os
import subprocess
import tempfile
from pathlib import Path

from idegym.utils.logging import get_logger
from jinja2 import Template

logger = get_logger(__name__)


def get_repo_root() -> Path:
    """Get the repository root directory."""
    return Path(__file__).parent.parent.parent


def build_orchestrator_image() -> None:
    """
    Build the orchestrator image and load it into minikube.
    Uses the existing build_orchestrator_image.py script from the main repo.
    """
    logger.info("Building orchestrator image...")

    repo_root = get_repo_root()
    script_path = repo_root / "scripts" / "build_orchestrator_image.py"

    if not script_path.exists():
        raise FileNotFoundError(f"Build script not found: {script_path}")

    # Use uv run to execute the script with its declared dependencies
    cmd = ["uv", "run", str(script_path), "--versions", "latest"]

    logger.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=repo_root, check=True)

    if result.returncode == 0:
        logger.info("✓ Orchestrator image built successfully")
    else:
        raise RuntimeError(f"Failed to build orchestrator image: exit code {result.returncode}")


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

    repo_root = get_repo_root()
    image_tag = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

    # Read and render the Dockerfile template
    dockerfile_template_path = repo_root / "Dockerfile.jinja"
    with open(dockerfile_template_path, "r") as f:
        template = Template(f.read())

    rendered_dockerfile = template.render(repository="docker.io/library", image="debian", tag="bookworm-20250520-slim")

    # Write rendered Dockerfile to a temporary file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".Dockerfile", delete=False) as tmp:
        tmp.write(rendered_dockerfile)
        tmp_dockerfile_path = tmp.name

    try:
        # Build the base server image
        cmd = ["docker", "build", "-t", image_tag, "-f", tmp_dockerfile_path, "."]

        logger.info(f"Building: {image_tag}")
        result = subprocess.run(cmd, cwd=repo_root, check=True)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to build server image: exit code {result.returncode}")

        logger.info("✓ Base server image built and available in local Docker")

        # Switch to default docker builder for local image access
        switch_to_default_docker_builder()

        # Load into minikube
        logger.info("Loading base image into minikube...")
        load_result = subprocess.run(
            ["minikube", "image", "load", image_tag],
            capture_output=True,
            text=True,
        )

        if load_result.returncode != 0:
            raise RuntimeError(f"Failed to load base image into minikube: {load_result.stderr}")

        logger.info("✓ Base server image loaded into minikube")

        return image_tag

    finally:
        # Clean up temporary Dockerfile
        if os.path.exists(tmp_dockerfile_path):
            os.unlink(tmp_dockerfile_path)


def build_all_images() -> None:
    """Build all required images for e2e testing."""
    logger.info("Building all required images...")

    build_orchestrator_image()
    build_base_server_image()

    logger.info("✓ All images built successfully")
