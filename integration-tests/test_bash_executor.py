import os
from pathlib import Path

import pytest
from idegym.backend.utils.bash_executor import BashCommandExecutionTimeoutError, BashExecutor


@pytest.mark.docker
class TestBashExecutor:
    """
    Real integration tests for BashExecutor that use actual bash processes.
    These tests are designed to be run in a Docker container to ensure safety and
    consistent behavior across different platforms.

    These tests are marked with @pytest.mark.docker and will run in CI environments
    but will be skipped in local environments unless --run-docker is provided.
    """

    @pytest.mark.asyncio
    async def test_execute_valid_command(self):
        """Test executing a valid command."""
        executor = BashExecutor()
        stdout, stderr, exit_code = await executor.execute_bash_command("echo 'hello world'")

        assert "hello world" in stdout
        assert stderr == ""
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_execute_command_with_stderr(self):
        """Test executing a command that produces stderr output."""
        executor = BashExecutor()
        stdout, stderr, exit_code = await executor.execute_bash_command("ls /nonexistent")

        assert stdout == ""
        assert "No such file or directory" in stderr
        assert exit_code != 0

    @pytest.mark.asyncio
    async def test_execute_command_with_non_zero_exit_code(self):
        """Test executing a command that returns a non-zero exit code."""
        executor = BashExecutor()
        stdout, stderr, exit_code = await executor.execute_bash_command("invalidcommand")

        assert stdout == ""
        assert "command not found" in stderr.lower() or "not found" in stderr.lower()
        assert exit_code != 0

    @pytest.mark.asyncio
    async def test_empty_command(self):
        """Test executing an empty command."""
        executor = BashExecutor()
        stdout, stderr, exit_code = await executor.execute_bash_command("")

        assert stdout == ""
        assert "syntax error" in stderr.lower()
        assert exit_code != 0

    @pytest.mark.asyncio
    async def test_execute_command_with_working_directory(self):
        """Test executing a command with a specific working directory."""
        temp_dir = Path("/tmp/bash_test")
        os.makedirs(temp_dir, exist_ok=True)

        executor = BashExecutor(working_directory=temp_dir)
        command = "pwd"
        stdout, stderr, exit_code = await executor.execute_bash_command(command)

        assert str(temp_dir) in stdout
        assert stderr == ""
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_exit_command(self):
        """Test executing an exit command."""
        executor = BashExecutor()
        stdout, stderr, exit_code = await executor.execute_bash_command("exit")

        assert stdout == ""
        assert stderr == ""
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_command_with_timeout(self):
        """Test that a command with a timeout raises the appropriate exception."""
        executor = BashExecutor()

        with pytest.raises(BashCommandExecutionTimeoutError):
            await executor.execute_bash_command("sleep 10", timeout=0.5)
