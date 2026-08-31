import pytest
from idegym.api.cpu import CpuQuantity
from idegym.api.memory import MemoryQuantity
from idegym.api.orchestrator.servers import ServerReuseStrategy
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.client.client import ServerCloseAction
from utils.constants import DEFAULT_SERVER_START_TIMEOUT
from utils.idegym_utils import create_http_client


@pytest.mark.asyncio
async def test_server_lifecycle_with_reuse(test_image, test_id):
    """
    Test complete server lifecycle:
    1. Start server with close_action="finish", run commands
    2. Server is finished and marked for reuse
    3. Start another server with same config and close_action="stop"
    4. Verify server is reused and filesystem is reset
    """
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
            resources=KubernetesResources(
                requests=ResourceQuantities(
                    cpu=CpuQuantity(millicores=500),
                    memory=MemoryQuantity(mi=500),
                    ephemeral_storage=MemoryQuantity(gi=1),
                ),
                limits=ResourceQuantities(
                    cpu=CpuQuantity(cores=1),
                    memory=MemoryQuantity(gi=1),
                    ephemeral_storage=MemoryQuantity(gi=2),
                ),
            ),
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            reuse_strategy=ServerReuseStrategy.RESTART,
            close_action=ServerCloseAction.FINISH,  # Mark for reuse
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
            resources=KubernetesResources(
                requests=ResourceQuantities(
                    cpu=CpuQuantity(millicores=500),
                    memory=MemoryQuantity(mi=500),
                    ephemeral_storage=MemoryQuantity(gi=1),
                ),
                limits=ResourceQuantities(
                    cpu=CpuQuantity(cores=1),
                    memory=MemoryQuantity(gi=1),
                    ephemeral_storage=MemoryQuantity(gi=2),
                ),
            ),
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            reuse_strategy=ServerReuseStrategy.RESTART,
            close_action=ServerCloseAction.STOP,  # Don't mark for reuse after this
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


@pytest.mark.asyncio
async def test_server_status_reports_a_live_server_and_survives_stopping_it(test_image, test_id):
    """Status has to answer for a stopped server too — that is the point of a liveness probe."""
    async with create_http_client(name=f"status-{test_id}", nodes_count=0) as client:
        server = await client.start_server(
            image_tag=test_image,
            server_name=f"status-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        )

        alive = await server.status()
        assert alive.usable is True
        assert alive.availability == "ALIVE"
        assert alive.pod_phase == "Running"
        assert alive.pod_ready is True
        assert alive.server_id == server.server_id

        await client.stop_server(server)

        stopped = await server.status()
        assert stopped.usable is False
        assert stopped.availability == "STOPPED"


@pytest.mark.asyncio
async def test_reading_status_does_not_count_as_activity(test_image, test_id):
    async with (
        create_http_client(name=f"status-idle-{test_id}", nodes_count=0) as client,
        client.with_server(
            image_tag=test_image,
            server_name=f"status-idle-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server,
    ):
        first = await server.status()
        second = await server.status()

        assert second.last_activity_at == first.last_activity_at
        assert second.idle_seconds >= first.idle_seconds
