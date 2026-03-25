"""Test complete server lifecycle: start, operations, finish, reuse."""

from uuid import uuid4

import pytest
from idegym.api.orchestrator.servers import ServerReuseStrategy
from kubernetes_asyncio.client import V1ResourceRequirements

from .idegym_utils import create_http_client


@pytest.mark.asyncio
async def test_server_lifecycle_with_reuse(test_image):
    """
    Test complete server lifecycle:
    1. Start server with close_action="finish", run commands
    2. Server is finished and marked for reuse
    3. Start another server with same config and close_action="stop"
    4. Verify server is reused and filesystem is reset
    """
    test_id = uuid4().hex[:8]

    async with (
        create_http_client(name=f"lifecycle-{test_id}", nodes_count=0, request_timeout_in_seconds=600) as client_a,
        create_http_client(name=f"lifecycle-{test_id}", nodes_count=0, request_timeout_in_seconds=600) as client_b,
    ):
        # Start first server with close_action="finish"
        async with client_a.with_server(
            image_tag=test_image,
            server_name=f"lifecycle-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=V1ResourceRequirements(
                requests={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                limits={"cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            ),
            server_start_wait_timeout_in_seconds=600,
            reuse_strategy=ServerReuseStrategy.RESTART,
            close_action="finish",  # Mark for reuse
        ) as server:
            server_id = server.server_id

            # Run command - install wget
            result = await server.execute_bash(
                script="apt-get update && apt-get install -y wget",
                command_timeout=300.0,
            )
            assert result.exit_code == 0

            # Verify wget installed
            result = await server.execute_bash(script="which wget", command_timeout=60.0)
            assert result.exit_code == 0

        # Start second server with same config and close_action="stop"
        async with client_b.with_server(
            image_tag=test_image,
            server_name=f"lifecycle-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            resources=V1ResourceRequirements(
                requests={"cpu": "500m", "memory": "500Mi", "ephemeral-storage": "1Gi"},
                limits={"cpu": "1", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            ),
            server_start_wait_timeout_in_seconds=600,
            reuse_strategy=ServerReuseStrategy.RESTART,
            close_action="stop",  # Don't mark for reuse after this
        ) as new_server:
            # Verify same server ID (reused)
            assert new_server.server_id == server_id

            # Verify wget NOT installed (server was reset with RESTART)
            result = await new_server.execute_bash(script="which wget", command_timeout=60.0)
            assert result.exit_code == 1

            # Test all reward operations
            result = await new_server.create_file(file_path="/tmp/test.txt", content="test content")
            assert result.status == "success"

            result = await new_server.test_reward(test_script="ls -l /tmp/test.txt")
            assert result.status == "success"

            result = await new_server.compilation_reward(compilation_script="echo 'Compilation test'")
            assert result.status == "success"

            result = await new_server.setup_reward(setup_check_script="python --version")
            assert result.status == "success"
