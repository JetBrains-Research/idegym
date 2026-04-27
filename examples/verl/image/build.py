"""Build the django-for-verl container image locally using Docker.

Supports two equivalent approaches — loading from the bundled YAML file or
constructing the image inline with the fluent API — and then builds it via
the local Docker daemon.

Usage::

    uv run python build.py            # build from YAML (default)
    uv run python build.py --push     # build and push to the registry
    uv run python build.py --fluent   # build using the inline fluent API
"""

from __future__ import annotations

import argparse
from pathlib import Path

from idegym.image.builder import Image
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.image.plugins import BaseSystem, IdeGYMServer, Project, User

IMAGE_NAME = "django-for-verl"
IMAGE_TAG = "ghcr.io/jetbrains-research/idegym/django-for-verl:rebuilt"

DJANGO_REF = "89807fbde8b7b17d00434bc4695535855e96fe77"
IDEGYM_REF = "2b18512dbb16301d6f97e40c8e4ae050ae2789bb"

SYSTEM_PACKAGES = (
    "bash",
    "ca-certificates",
    "coreutils",
    "curl",
    "dumb-init",
    "findutils",
    "git",
    "netcat-openbsd",
    "sudo",
    # Python build dependencies
    "clang",
    "build-essential",
    "libssl-dev",
    "zlib1g-dev",
    "libncurses5-dev",
    "libncursesw5-dev",
    "libreadline-dev",
    "libsqlite3-dev",
    "libgdbm-dev",
    "libdb5.3-dev",
    "libbz2-dev",
    "libexpat1-dev",
    "liblzma-dev",
    "tk-dev",
    "uuid-dev",
    "libmemcached-dev",
    "libffi-dev",
    "libjpeg-dev",
)

# All items are joined into a single `RUN set -eux; cmd1 && cmd2 && ...` block so
# shell variables assigned in earlier items are visible in later ones.
SETUP_COMMANDS = (
    # Pin Python version and standalone-build release
    "PYTHON_VERSION=3.10.13",
    "PYTHON_BUILD_STANDALONE_RELEASE=20240224",
    'MINOR="${PYTHON_VERSION%.*}"',
    # Create per-user directories where the standalone Python will live
    'mkdir -p "/home/appuser/.local/bin" "/home/appuser/.local/python/${PYTHON_VERSION}/bin"',
    # Make the Python binaries reachable for subsequent commands in this block
    'export PATH="/home/appuser/.local/python/${PYTHON_VERSION}/bin:/home/appuser/.local/bin:${PATH}"',
    # Download and install the python-build-standalone release
    "ARCH=$(uname -m)",
    'INSTALL_DIR="/home/appuser/.local/python/${PYTHON_VERSION}"',
    'DOWNLOADS="https://github.com/astral-sh/python-build-standalone/releases/download"',
    'curl -fsSL "${DOWNLOADS}/${PYTHON_BUILD_STANDALONE_RELEASE}/cpython-${PYTHON_VERSION}+${PYTHON_BUILD_STANDALONE_RELEASE}-${ARCH}-unknown-linux-gnu-install_only.tar.gz" -o "/tmp/${PYTHON_VERSION}.tar.gz"',
    'tar -xzf "/tmp/${PYTHON_VERSION}.tar.gz" -C "${INSTALL_DIR}" --strip-components=1',
    'rm "/tmp/${PYTHON_VERSION}.tar.gz"',
    # Create convenience symlinks under ~/.local/bin
    'USER_BIN_DIR="/home/appuser/.local/bin"',
    'ln -frs "${INSTALL_DIR}/bin/python${MINOR}" "${INSTALL_DIR}/bin/python"',
    'ln -frs "${INSTALL_DIR}/bin/pip${MINOR}" "${INSTALL_DIR}/bin/pip"',
    'ln -frs "${INSTALL_DIR}/bin/pip${MINOR}" "${USER_BIN_DIR}/pip${MINOR}"',
    'ln -frs "${INSTALL_DIR}/bin/pip3" "${USER_BIN_DIR}/pip3"',
    'ln -frs "${INSTALL_DIR}/bin/pip" "${USER_BIN_DIR}/pip"',
    'ln -frs "${INSTALL_DIR}/bin/python${MINOR}" "${USER_BIN_DIR}/python${MINOR}"',
    'ln -frs "${INSTALL_DIR}/bin/python3" "${USER_BIN_DIR}/python3"',
    'ln -frs "${INSTALL_DIR}/bin/python" "${USER_BIN_DIR}/python"',
    # Install project dependencies ($IDEGYM_PROJECT_ROOT is set by the project plugin)
    'cd "${IDEGYM_PROJECT_ROOT}/tests" && pip3 install --user --upgrade setuptools wheel',
    'python3 -m pip install -e "${IDEGYM_PROJECT_ROOT}"',
    'python3 -m pip install -r "${IDEGYM_PROJECT_ROOT}/tests/requirements/py3.txt"',
)


def build_from_yaml() -> Image:
    """Load the image definition from the bundled YAML file."""
    yaml_path = Path(__file__).parent / "django.yaml"
    (image,) = Image.load_all(yaml_path.read_bytes())
    return image


def build_from_fluent() -> Image:
    """Construct the same image definition using the fluent builder API."""
    return (
        Image.from_base("debian:bookworm-slim", name=IMAGE_NAME)
        .with_plugin(BaseSystem(packages=SYSTEM_PACKAGES))
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(
            Project.from_git_clone(
                url="https://github.com/django/django.git",
                ref=DJANGO_REF,
                owner="appuser",
            )
        )
        # IdeGYMServer must be last — it emits ENTRYPOINT / CMD / HEALTHCHECK.
        .with_plugin(IdeGYMServer.from_git(url="https://github.com/JetBrains-Research/idegym.git", ref=IDEGYM_REF))
        .run_commands(*SETUP_COMMANDS)
        .with_runtime(
            resources={
                "requests": {"cpu": "1000m", "memory": "1024Mi", "ephemeral-storage": "2560Mi"},
                "limits": {"cpu": "4000m", "memory": "4096Mi", "ephemeral-storage": "5120Mi"},
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the django-for-verl container image.")
    parser.add_argument("--push", action="store_true", help="Push the image after building.")
    parser.add_argument("--fluent", action="store_true", help="Use the inline fluent API instead of YAML.")
    args = parser.parse_args()

    image = build_from_fluent() if args.fluent else build_from_yaml()

    api = IdeGYMDockerAPI()
    built = api.build_image(image, push=args.push)

    # Re-tag with the human-friendly :latest alias used by the verl training config.
    built.tag(IMAGE_TAG)
    if args.push:
        api.push(built)

    print(f"Built and tagged as {IMAGE_TAG}")


if __name__ == "__main__":
    main()
