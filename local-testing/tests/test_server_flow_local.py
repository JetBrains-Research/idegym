import subprocess
from uuid import uuid4

import pytest
from idegym.api.docker import BaseImage
from idegym.api.git import GitRepositorySnapshot
from idegym.api.orchestrator.servers import ServerReuseStrategy
from idegym.client import IdeGYMDockerAPI
from idegym.utils.logging import get_logger
from kubernetes_asyncio.client import V1ResourceRequirements
from tests.utils import create_http_client

logger = get_logger(__name__)


def build_and_load_test_image() -> str:
    """
    Build a test image using IdeGYMDockerAPI and load it into minikube.
    Returns the image tag.
    """
    logger.info("Building test image using IdeGYMDockerAPI")

    # Create docker API without registry (local build only)
    docker_api = IdeGYMDockerAPI()

    # Build image for python-scripts repo
    project = GitRepositorySnapshot(
        repository={
            "server": "github.com",
            "owner": "realpython",
            "name": "python-scripts",
        },
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

    image = docker_api.build(
        project=project,
        base=BaseImage.DEBIAN,
        commands=commands,
    )

    image_tag = str(image.repo_tags[0])
    logger.info(f"Image built with tag: {image_tag}")

    # Load image into minikube
    logger.info(f"Loading image into minikube: {image_tag}")
    result = subprocess.run(
        ["minikube", "image", "load", image_tag],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error(f"Failed to load image into minikube: {result.stderr}")
        raise RuntimeError(f"Failed to load image into minikube: {result.stderr}")

    logger.info(f"Image loaded into minikube successfully: {image_tag}")
    return image_tag


async def async_test_server_flow_with_local_image():
    """
    Test server flow using a locally built image:
    1. Builds image using IdeGYMDockerAPI and loads into minikube
    2. Registers 2 clients with the same name
    3. Starts a server using the built image
    4. Runs a test command
    5. Finishes the server
    6. Starts another server with the same image
    7. Verifies server ID is reused
    8. Verifies previous changes are not present (server was reset)
    """
    logger.info("Starting server flow test with local image")

    # Build and load test image
    test_image_tag = build_and_load_test_image()

    test_id = uuid4().hex[:8]
    logger.info(f"Generated test ID: {test_id}")

    async with (
        create_http_client(
            name=f"local-flow-test-{test_id}", nodes_count=0, request_timeout_in_seconds=600
        ) as client_a,
        create_http_client(
            name=f"local-flow-test-{test_id}", nodes_count=0, request_timeout_in_seconds=600
        ) as client_b,
    ):
        # 1. Register clients
        logger.info(f"Registering 2 clients 'local-flow-test-{test_id}'")
        client_id_a, client_id_b = client_a.client_id, client_b.client_id
        logger.info(f"Client a registered with ID: {client_id_a}")
        logger.info(f"Client b registered with ID: {client_id_b}")

        assert client_id_a is not None, "Client ID of client_A should not be None"
        assert client_id_b is not None, "Client ID of client_B should not be None"

        client = client_a

        # 2. Start a server using the built image
        logger.info(f"Starting server with image: {test_image_tag}")
        async with client.with_server(
            image_tag=test_image_tag,
            server_name=f"local-flow-test-{test_id}-server",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=V1ResourceRequirements(
                requests={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                limits={"cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            ),
            server_start_wait_timeout_in_seconds=600,
            reuse_strategy=ServerReuseStrategy.RESTART,
        ) as server:
            logger.info(f"Server started: {server}")
            server_id = server.server_id
            logger.info(f"Server started with ID: {server_id}")
            assert server_id is not None, "Server ID should not be None"

            # 3. Run a test command - install wget
            logger.info(f"Running 'apt-get update && apt-get install -y wget' on server {server_id}")
            result = await server.execute_bash(
                script="apt-get update && apt-get install -y wget",
                command_timeout=300.0,
            )

            logger.info(f"Command execution result: {result}")
            assert result.exit_code == 0, f"Failed to install wget: {result}"
            logger.info("wget installed successfully")

            # Verify wget is installed
            result = await server.execute_bash(
                script="which wget",
                command_timeout=60.0,
            )
            assert result.exit_code == 0, "wget should be installed"
            logger.info("Verified wget is installed")

        # 4. Start another server with the same configuration
        logger.info(f"Starting another server with the same image tag: {test_image_tag}")
        async with client.with_server(
            image_tag=test_image_tag,
            server_name=f"local-flow-test-{test_id}-server",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=V1ResourceRequirements(
                requests={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                limits={"cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            ),
            server_start_wait_timeout_in_seconds=600,
            reuse_strategy=ServerReuseStrategy.RESTART,
        ) as new_server:
            new_server_id = new_server.server_id
            logger.info(f"New server started with ID: {new_server_id}")

            # 5. Check that the server ID is the same (server was reused)
            logger.info(f"Checking if server IDs match: {server_id} == {new_server_id}")
            assert new_server_id == server_id, f"Server IDs do not match: {server_id} != {new_server_id}"
            logger.info("Server IDs match - server was reused as expected")

            # 6. Verify wget is NOT installed (server was reset)
            logger.info("Verifying wget is not installed on restarted server")
            result = await new_server.execute_bash(
                script="which wget",
                command_timeout=60.0,
            )

            logger.info(f"Wget check result: {result}")
            assert result.exit_code == 1, f"Wget should not be found on restarted server: {result}"
            logger.info("Wget not found on restarted server - server was reset as expected")

            # 7. Test file operations
            logger.info("Testing file creation")
            result = await new_server.create_file(file_path="/tmp/test.txt", content="test content")
            assert result.status == "success", f"File creation failed: {result}"
            logger.info(f"File creation result: {result}")

            # 8. Test reward operations
            logger.info("Testing test reward")
            result = await new_server.test_reward(test_script="ls -l /tmp/test.txt")
            assert result.status == "success", f"Test reward failed: {result}"
            logger.info(f"Test reward result: {result}")

            logger.info("Testing compilation reward")
            result = await new_server.compilation_reward(compilation_script="echo 'Compilation test'")
            assert result.status == "success", f"Compilation reward failed: {result}"
            logger.info(f"Compilation reward result: {result}")

            logger.info("Testing setup reward")
            result = await new_server.setup_reward(setup_check_script="python --version")
            assert result.status == "success", f"Setup reward failed: {result}"
            logger.info(f"Setup reward result: {result}")

    logger.info("Server flow test completed successfully")


@pytest.mark.asyncio
async def test_server_flow_with_local_image():
    """Test server lifecycle with a locally built and loaded image."""
    await async_test_server_flow_with_local_image()
