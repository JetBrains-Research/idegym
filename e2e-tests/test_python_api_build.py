"""
E2E tests for building and deploying images using the Python fluent API.

Two build paths are covered:

1. Kaniko (orchestrator) — Image.write_yaml() → client.jobs.build_and_push_images()
   Builds inside the cluster via Kaniko, pushes to the in-cluster registry, and then
   pulls the image into containerd before starting a server.

2. Local Docker — IdeGYMDockerAPI.build_image() → minikube image load
   Builds on the developer machine using the local Docker daemon, loads the result
   directly into minikube's containerd, and starts a server without touching the
   in-cluster registry at all.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest
from from_root import from_root
from idegym.api.status import Status
from idegym.image.builder import Image
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.plugins.defaults.image import BaseSystem, IdeGYMServer, Permissions, Project, User
from kubernetes_asyncio.client import V1ResourceRequirements
from utils.constants import (
    DEFAULT_NAMESPACE,
    DEFAULT_SERVER_START_TIMEOUT,
    PULL_LOCAL_REGISTRY_HOST,
    PUSH_LOCAL_REGISTRY_HOST,
)
from utils.idegym_utils import create_http_client

# Used by Kaniko (in-cluster) builds — only reachable from inside minikube
_BASE_IMAGE = "registry.kube-system.svc.cluster.local/server-debian-bookworm-20250520-slim:latest"
# Used by local Docker builds — available in the host Docker daemon after build_all_images()
_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"
_PROJECT_URL = "https://github.com/realpython/python-scripts.git"
_PROJECT_REF = "cb448c2dc3593dbfbe1ca47b49193b320115aae5"
_DEFAULT_RESOURCES = V1ResourceRequirements(
    requests={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
    limits={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
)


def _to_runtime_tag(tag: str) -> str:
    """Convert a push-registry tag to the pull-registry tag used by containerd."""
    return tag.replace(PUSH_LOCAL_REGISTRY_HOST, PULL_LOCAL_REGISTRY_HOST, 1)


@pytest.mark.asyncio
async def test_python_api_build_and_deploy(test_id, kaniko_image_loader):
    """
    Build an image using the Python fluent API, deploy it as a server, and verify
    that User, Project, and Permissions plugin effects are present in the container.

    Flow:
    1. Construct an Image object using the fluent API with User + Project + Permissions
    2. Write it to a temporary YAML file via Image.write_yaml()
    3. Submit the YAML to the orchestrator for a Kaniko build
    4. Load the built image into containerd
    5. Deploy a server and verify:
       - devuser (uid=4000) was created by the User plugin
       - /home/devuser is owned by devuser (Permissions plugin)
       - project files were downloaded to /home/appuser/work (Project plugin)
       - marker file was written during the commands block
    """
    image = (
        Image.from_base(_BASE_IMAGE)
        .with_plugin(User(username="devuser", uid=4000, gid=4000, sudo=True))
        .with_plugin(
            Permissions(
                paths={
                    "/home/devuser": {"owner": "devuser", "mode": "755"},
                }
            )
        )
        .with_plugin(
            Project.from_git(
                url=_PROJECT_URL,
                ref=_PROJECT_REF,
                owner="appuser",
                target="/home/appuser/work",
            )
        )
        .run_commands(
            # Write to devuser's home (not /tmp — gVisor mounts /tmp as tmpfs, wiping build-time files)
            "echo 'python-api-test' > /home/devuser/python-api-test.txt",
        )
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        yaml_path = image.write_yaml(Path(tmp_dir) / "image.yaml")

        async with create_http_client(
            name=f"python-api-{test_id}",
            nodes_count=0,
            request_timeout_in_seconds=600,
        ) as client:
            build_summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace=DEFAULT_NAMESPACE,
                timeout=600,
                poll_interval=10,
            )

            assert build_summary.total_jobs == 1, f"Expected 1 job, got {build_summary.total_jobs}"
            assert build_summary.failed_jobs == 0, f"Build failed: {build_summary.jobs_results[0].details}"

            job_result = build_summary.jobs_results[0]
            assert job_result.status == Status.SUCCESS, f"Build status: {job_result.status}"
            assert job_result.tag is not None, "No image tag returned"

            image_tag = _to_runtime_tag(job_result.tag)
            await kaniko_image_loader(image_tag)

            async with client.with_server(
                image_tag=image_tag,
                server_name=f"python-api-server-{test_id}",
                runtime_class_name="gvisor",
                run_as_root=True,
                resources=_DEFAULT_RESOURCES,
                server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            ) as server:
                # User plugin: devuser with uid=4000 should exist
                result = await server.execute_bash(script="id devuser", command_timeout=60.0)
                assert result.exit_code == 0, f"devuser not found: {result.stderr}"
                assert "4000" in result.stdout, f"Unexpected uid for devuser: {result.stdout}"

                # Permissions plugin: /home/devuser should be owned by devuser
                result = await server.execute_bash(script="stat -c '%U' /home/devuser", command_timeout=60.0)
                assert result.exit_code == 0, f"Failed to stat /home/devuser: {result.stderr}"
                assert "devuser" in result.stdout, f"Unexpected owner of /home/devuser: {result.stdout}"

                # Project plugin: project files should be at /home/appuser/work
                result = await server.execute_bash(script="ls /home/appuser/work/", command_timeout=60.0)
                assert result.exit_code == 0, f"Project directory missing: {result.stderr}"
                assert result.stdout.strip(), "Project directory is empty"

                # Commands block: marker file should exist in devuser's home
                result = await server.execute_bash(script="cat /home/devuser/python-api-test.txt", command_timeout=60.0)
                assert result.exit_code == 0, f"Marker file missing: {result.stderr}"
                assert "python-api-test" in result.stdout, f"Unexpected content: {result.stdout}"


@pytest.mark.asyncio
async def test_python_api_base_system_plugin(test_id, kaniko_image_loader):
    """
    Build an image using the Python API with a BaseSystem plugin that installs a
    custom package (tree), and verify it is present in the running container.
    """
    image = (
        Image.from_base(_BASE_IMAGE)
        .with_plugin(BaseSystem(packages=("ca-certificates", "curl", "tree")))
        # Write to /home/appuser — commands run as root (no User plugin), and /tmp is tmpfs in gVisor
        .run_commands("echo 'base-system-api-test' > /home/appuser/base-system-api-test.txt")
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        yaml_path = image.write_yaml(Path(tmp_dir) / "image.yaml")

        async with create_http_client(
            name=f"python-api-bs-{test_id}",
            nodes_count=0,
            request_timeout_in_seconds=600,
        ) as client:
            build_summary = await client.jobs.build_and_push_images(
                path=yaml_path,
                namespace=DEFAULT_NAMESPACE,
                timeout=600,
                poll_interval=10,
            )

            assert build_summary.total_jobs == 1
            assert build_summary.failed_jobs == 0, f"Build failed: {build_summary.jobs_results[0].details}"

            job_result = build_summary.jobs_results[0]
            assert job_result.status == Status.SUCCESS
            assert job_result.tag is not None

            image_tag = _to_runtime_tag(job_result.tag)
            await kaniko_image_loader(image_tag)

            async with client.with_server(
                image_tag=image_tag,
                server_name=f"python-api-bs-server-{test_id}",
                runtime_class_name="gvisor",
                run_as_root=True,
                resources=_DEFAULT_RESOURCES,
                server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            ) as server:
                # Verify tree was installed
                result = await server.execute_bash(script="which tree", command_timeout=60.0)
                assert result.exit_code == 0, f"tree not installed: {result.stderr}"

                # Verify marker file
                result = await server.execute_bash(
                    script="cat /home/appuser/base-system-api-test.txt", command_timeout=60.0
                )
                assert result.exit_code == 0
                assert "base-system-api-test" in result.stdout


@pytest.mark.asyncio
async def test_local_docker_build_and_deploy(test_id):
    """
    Build an image with the Python fluent API using the local Docker daemon
    (not Kaniko), load it directly into minikube, and verify it runs correctly
    as an IDEGym server.

    This exercises a completely different build path from the Kaniko tests:
      Image.build_image()  →  docker build (local)
      minikube image load  →  containerd (no registry involved)
      client.with_server() →  server pod starts from the local image

    The base image _LOCAL_BASE_IMAGE is built during the session setup
    (build_all_images → build_base_server_image) and is available in the
    host Docker daemon before any test runs.
    """
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"local-docker-test-{test_id}")
        .with_plugin(User(username="localuser", uid=5000, gid=5000, sudo=True))
        .with_plugin(
            Permissions(
                paths={
                    "/home/localuser": {"owner": "localuser", "mode": "755"},
                }
            )
        )
        # Commands run as localuser (User plugin sets ctx.current_user).
        # Write to localuser's home — /tmp is a tmpfs in gVisor and is wiped on start.
        .run_commands("echo 'local-docker-test' > /home/localuser/local-test.txt")
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    # Build with the local Docker daemon
    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])

    # Load into minikube's containerd so pods can use it
    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=120,
    )

    async with create_http_client(
        name=f"local-docker-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=300,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"local-docker-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            # User plugin: localuser with uid=5000 should exist
            result = await server.execute_bash(script="id localuser", command_timeout=60.0)
            assert result.exit_code == 0, f"localuser not found: {result.stderr}"
            assert "5000" in result.stdout, f"Unexpected uid for localuser: {result.stdout}"

            # Permissions plugin: /home/localuser should be owned by localuser
            result = await server.execute_bash(script="stat -c '%U' /home/localuser", command_timeout=60.0)
            assert result.exit_code == 0, f"Failed to stat /home/localuser: {result.stderr}"
            assert "localuser" in result.stdout, f"Unexpected owner: {result.stdout}"

            # Commands block: marker file should be in localuser's home
            result = await server.execute_bash(script="cat /home/localuser/local-test.txt", command_timeout=60.0)
            assert result.exit_code == 0, f"Marker file missing: {result.stderr}"
            assert "local-docker-test" in result.stdout, f"Unexpected content: {result.stdout}"


@pytest.mark.asyncio
async def test_local_docker_build_from_idegym_server_plugin(test_id):
    """
    Build an IdeGYM server image from a plain Debian base using IdeGYMServer.from_local(),
    without relying on a pre-built server image that already has IdeGYM code inside.

    Build path:
      debian:bookworm-20250520-slim
        → BaseSystem   (apt packages: dumb-init, netcat-openbsd, etc.)
        → User         (create appuser uid=1000)
        → IdeGYMServer.from_local()  (copy local source, uv sync, supervisor)
      IdeGYMDockerAPI.build_image()  →  minikube image load  →  client.with_server()

    Verifies that:
    - IdeGYM is installed at $IDEGYM_PATH from local source
    - The Python virtual environment was created by uv sync
    - The server starts and accepts requests
    """
    image = (
        Image.from_base("debian:bookworm-20250520-slim")
        .named(f"idegym-from-local-{test_id}")
        .with_plugin(BaseSystem())
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        .with_runtime(
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                "limits": {"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
            },
        )
    )

    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])

    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=120,
    )

    async with create_http_client(
        name=f"idegym-from-local-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=300,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"idegym-from-local-server-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=_DEFAULT_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            # IdeGYM should be installed at /opt/idegym from local source
            result = await server.execute_bash(script="ls /opt/idegym/server", command_timeout=30.0)
            assert result.exit_code == 0, f"IdeGYM server dir missing: {result.stderr}"
            assert result.stdout.strip(), "IdeGYM server directory is empty"

            # uv sync should have created a virtual environment
            result = await server.execute_bash(script="ls /opt/idegym/.venv/bin/python", command_timeout=30.0)
            assert result.exit_code == 0, f"Python venv not found: {result.stderr}"
