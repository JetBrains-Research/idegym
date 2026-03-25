"""Test different server reuse strategies and advanced operations."""

from uuid import uuid4

import pytest
from idegym.api.orchestrator.servers import ServerReuseStrategy
from idegym.client.client import ServerCloseAction
from kubernetes_asyncio.client import V1ResourceRequirements

from .utils import create_http_client


@pytest.mark.asyncio
async def test_reuse_strategy_reset_vs_restart(test_image):
    """Test RESET (no pod restart) vs RESTART (clears filesystem)."""
    test_id = uuid4().hex[:8]

    async with create_http_client(name=f"reuse-{test_id}", nodes_count=0) as client:
        # RESET: Reuses pod, preserves filesystem
        async with client.with_server(
            image_tag=test_image,
            server_name=f"reset-{test_id}",
            runtime_class_name="gvisor",
            reuse_strategy=ServerReuseStrategy.RESET,
            close_action=ServerCloseAction.FINISH,
        ) as srv:
            id1 = srv.server_id
            await srv.create_file(file_path="/tmp/reset.txt", content="data")

        async with client.with_server(
            image_tag=test_image,
            server_name=f"reset-{test_id}",
            runtime_class_name="gvisor",
            reuse_strategy=ServerReuseStrategy.RESET,
            close_action=ServerCloseAction.STOP,
        ) as srv:
            assert srv.server_id == id1
            result = await srv.execute_bash(script="cat /tmp/reset.txt")
            assert result.exit_code == 0, "RESET should preserve filesystem"

        # RESTART: Reuses pod, clears filesystem
        async with client.with_server(
            image_tag=test_image,
            server_name=f"restart-{test_id}",
            runtime_class_name="gvisor",
            reuse_strategy=ServerReuseStrategy.RESTART,
            close_action=ServerCloseAction.FINISH,
        ) as srv:
            id2 = srv.server_id
            await srv.create_file(file_path="/tmp/restart.txt", content="data")

        async with client.with_server(
            image_tag=test_image,
            server_name=f"restart-{test_id}",
            runtime_class_name="gvisor",
            reuse_strategy=ServerReuseStrategy.RESTART,
            close_action=ServerCloseAction.STOP,
        ) as srv:
            assert srv.server_id == id2
            result = await srv.execute_bash(script="cat /tmp/restart.txt")
            assert result.exit_code != 0, "RESTART should clear filesystem"


@pytest.mark.asyncio
async def test_reuse_strategy_none(test_image):
    """Test NONE strategy creates new servers."""
    test_id = uuid4().hex[:8]

    async with create_http_client(name=f"none-{test_id}", nodes_count=0) as client:
        async with client.with_server(
            image_tag=test_image,
            server_name=f"none-{test_id}",
            runtime_class_name="gvisor",
            reuse_strategy=ServerReuseStrategy.NONE,
            close_action=ServerCloseAction.FINISH,
        ) as srv:
            id1 = srv.server_id

        async with client.with_server(
            image_tag=test_image,
            server_name=f"none-{test_id}",
            runtime_class_name="gvisor",
            reuse_strategy=ServerReuseStrategy.NONE,
            close_action=ServerCloseAction.STOP,
        ) as srv:
            assert srv.server_id != id1, "NONE should create new servers"


@pytest.mark.asyncio
async def test_reset_project(test_image):
    """
    Test reset_project resets changes in the cloned repository.
    Changes made to files in the work directory should be reverted.
    """
    test_id = uuid4().hex[:8]

    async with create_http_client(
        name=f"reset-proj-{test_id}", nodes_count=0, request_timeout_in_seconds=120
    ) as client:
        async with client.with_server(
            image_tag=test_image,
            server_name=f"reset-proj-{test_id}",
            runtime_class_name="gvisor",
            close_action=ServerCloseAction.STOP,
        ) as server:
            # Modify a file in the cloned repo
            result = await server.execute_bash(script="ls /home/appuser/work", command_timeout=30.0)
            assert result.exit_code == 0

            # Create a new file in the work directory
            await server.create_file(file_path="/home/appuser/work/new_file.txt", content="new content")
            result = await server.execute_bash(script="cat /home/appuser/work/new_file.txt", command_timeout=30.0)
            assert result.exit_code == 0 and "new content" in result.stdout

            # Modify an existing file in the repo
            await server.execute_bash(script="echo 'MODIFIED' >> /home/appuser/work/README.md", command_timeout=30.0)
            result = await server.execute_bash(
                script="grep MODIFIED /home/appuser/work/README.md", command_timeout=30.0
            )
            assert result.exit_code == 0

            # Reset the project
            reset_result = await server.reset_project(reset_timeout=30.0)
            assert reset_result.status == "success"

            # Verify new file is gone
            result = await server.execute_bash(script="cat /home/appuser/work/new_file.txt", command_timeout=30.0)
            assert result.exit_code != 0, "New file should be removed after reset"

            # Verify modifications are reverted
            result = await server.execute_bash(
                script="grep MODIFIED /home/appuser/work/README.md", command_timeout=30.0
            )
            assert result.exit_code != 0, "Modifications should be reverted after reset"


@pytest.mark.asyncio
async def test_resource_limits_enforcement(test_image):
    """
    Test that resource limits are enforced.
    Try to create a server with excessive resources (100 CPU, 1000Gi memory).
    Should fail with 429 or timeout after retrying 429 errors.
    """
    test_id = uuid4().hex[:8]

    async with create_http_client(name=f"limits-{test_id}", nodes_count=0, request_timeout_in_seconds=60) as client:
        # Try server with excessive resources - should fail
        resource_limit_hit = False
        try:
            async with client.with_server(
                image_tag=test_image,
                server_name=f"limits-excessive-{test_id}",
                runtime_class_name="gvisor",
                resources=V1ResourceRequirements(
                    requests={"cpu": "100", "memory": "1000Gi"},  # Excessive resources
                    limits={"cpu": "100", "memory": "1000Gi"},
                ),
                server_start_wait_timeout_in_seconds=60,
                close_action=ServerCloseAction.STOP,
            ) as _:
                # Should not reach here
                pass
        except (TimeoutError, Exception) as e:
            error_msg = str(e)
            # Accept either TimeoutError (from retrying 429s) or direct resource limit errors
            if (
                "429" in error_msg
                or "Resource limit" in error_msg
                or "limit exceeded" in error_msg.lower()
                or "timed out" in error_msg.lower()
            ):
                resource_limit_hit = True
            else:
                # Re-raise if it's not a resource limit related error
                raise

        assert resource_limit_hit, "Expected to hit resource limit with excessive resource request"


@pytest.mark.asyncio
async def test_concurrent_clients(test_image):
    """Test multiple clients with concurrent servers."""
    test_id = uuid4().hex[:8]

    async with (
        create_http_client(name=f"c1-{test_id}", nodes_count=0) as c1,
        create_http_client(name=f"c2-{test_id}", nodes_count=0) as c2,
    ):
        async with (
            c1.with_server(
                image_tag=test_image,
                server_name=f"s1-{test_id}",
                runtime_class_name="gvisor",
                close_action=ServerCloseAction.STOP,
            ) as s1,
            c2.with_server(
                image_tag=test_image,
                server_name=f"s2-{test_id}",
                runtime_class_name="gvisor",
                close_action=ServerCloseAction.STOP,
            ) as s2,
        ):
            assert s1.server_id != s2.server_id


@pytest.mark.asyncio
async def test_bash_and_file_operations(test_image):
    """Test bash execution and file operations."""
    test_id = uuid4().hex[:8]

    async with create_http_client(name=f"ops-{test_id}", nodes_count=0) as client:
        async with client.with_server(
            image_tag=test_image,
            server_name=f"ops-{test_id}",
            runtime_class_name="gvisor",
            close_action=ServerCloseAction.STOP,
        ) as server:
            # Bash: success
            result = await server.execute_bash(script="echo 'ok'")
            assert result.exit_code == 0 and "ok" in result.stdout

            # Bash: failure
            result = await server.execute_bash(script="exit 42")
            assert result.exit_code == 42

            # Bash: stderr
            result = await server.execute_bash(script="echo 'err' >&2")
            assert "err" in result.stderr

            # File operations
            await server.create_file(file_path="/tmp/test.py", content="print('hello')")
            result = await server.execute_bash(script="python /tmp/test.py")
            assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_reward_operations(test_image):
    """Test setup, compilation, and test rewards."""
    test_id = uuid4().hex[:8]

    async with create_http_client(name=f"reward-{test_id}", nodes_count=0) as client:
        async with client.with_server(
            image_tag=test_image,
            server_name=f"reward-{test_id}",
            runtime_class_name="gvisor",
            close_action=ServerCloseAction.STOP,
        ) as server:
            result = await server.setup_reward(setup_check_script="python --version")
            assert result.status == "success"

            result = await server.compilation_reward(compilation_script="echo 'compiled'")
            assert result.status == "success"

            result = await server.test_reward(test_script="python -c 'assert 1+1==2'")
            assert result.status == "success"
