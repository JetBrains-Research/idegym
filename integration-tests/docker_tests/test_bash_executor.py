import asyncio
import contextlib
import os
import shlex
import signal
import sys
from pathlib import Path

import psutil
import pytest
from idegym.backend.utils.bash_executor import BashCommandExecutionTimeoutError, BashExecutor
from structlog.testing import capture_logs


def _detached_child_command(child_pid_path: Path) -> str:
    child_script = (
        "import os,pathlib,time; "
        "os.setsid(); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(10)"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(child_script)} & wait"


async def _wait_for_pid_file(child_pid_path: Path, timeout: float = 1.0) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not child_pid_path.exists():
        if loop.time() >= deadline:
            raise AssertionError(f"Child did not write its PID to {child_pid_path}")
        await asyncio.sleep(0.01)
    return int(child_pid_path.read_text())


def _process_is_running(process_id: int) -> bool:
    try:
        process = psutil.Process(process_id)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _kill_process_if_running(process_id: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(process_id, signal.SIGKILL)


class TestBashExecutor:
    """Real integration tests for BashExecutor that use actual bash processes."""

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
    async def test_output_is_returned_byte_for_byte(self):
        executor = BashExecutor()
        stdout, _stderr, exit_code = await executor.execute_bash_command("printf '  padded  \\n\\n'")

        assert stdout == "  padded  \n\n"
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_strip_output_trims_surrounding_whitespace(self):
        executor = BashExecutor()
        stdout, _stderr, exit_code = await executor.execute_bash_command("printf '  padded  \\n\\n'", strip_output=True)

        assert stdout == "padded"
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_non_utf8_output_is_replaced_and_exit_code_survives(self):
        executor = BashExecutor()
        stdout, _stderr, exit_code = await executor.execute_bash_command("printf '\\xff\\xfe'; exit 3")

        assert stdout == "��"
        assert exit_code == 3

    @pytest.mark.asyncio
    async def test_command_with_timeout(self):
        """Test that a command with a timeout raises the appropriate exception."""
        executor = BashExecutor()

        with pytest.raises(BashCommandExecutionTimeoutError):
            await executor.execute_bash_command("sleep 10", timeout=0.5)

    @pytest.mark.asyncio
    async def test_bounded_stdout_and_stderr_preserve_head_tail_and_exit_code(self):
        executor = BashExecutor()

        stdout, stderr, exit_code = await executor.execute_bash_command(
            "(printf '%200000sTAIL' '' | tr ' ' H) & (printf '%200000sTAIL' '' | tr ' ' E >&2) & wait; exit 7",
            max_output_bytes=64,
        )

        assert stdout.startswith("H" * 32)
        assert stdout.endswith("H" * 28 + "TAIL")
        assert "IdeGYM truncated 199940 output bytes" in stdout
        assert stderr.startswith("E" * 32)
        assert stderr.endswith("E" * 28 + "TAIL")
        assert "IdeGYM truncated 199940 output bytes" in stderr
        assert exit_code == 7

    @pytest.mark.asyncio
    async def test_unlimited_output_and_decoding(self):
        executor = BashExecutor()

        stdout, _, exit_code = await executor.execute_bash_command(
            "printf '  indented\\377\\n'",
            max_output_bytes=None,
        )

        assert stdout == "  indented�\n"
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_timeout_logs_partial_bounded_output_and_reaps_process(self):
        executor = BashExecutor()

        with capture_logs() as logs, pytest.raises(BashCommandExecutionTimeoutError):
            await executor.execute_bash_command(
                "printf 'partial output'; sleep 10",
                timeout=0.05,
                max_output_bytes=64,
            )

        timeout_log = next(entry for entry in logs if entry["event"] == "Command execution timed out")
        assert timeout_log["log_level"] == "warning"
        assert timeout_log["stdout_bytes"] == len("partial output")
        assert "stdout" not in timeout_log

        partial_log = next(entry for entry in logs if entry["event"] == "Partial output of the timed-out command")
        assert partial_log["log_level"] == "debug"
        assert partial_log["stdout"] == "partial output"
        assert partial_log["stderr"] == ""

    @pytest.mark.asyncio
    async def test_info_logs_carry_no_command_text_or_output(self):
        executor = BashExecutor()

        with capture_logs() as logs:
            await executor.execute_bash_command("export TOKEN=s3cr3t; printf 'result'")

        info_logs = [entry for entry in logs if entry["log_level"] == "info"]
        assert "s3cr3t" not in repr(info_logs)
        assert "result" not in repr(info_logs)

        completed = next(entry for entry in info_logs if entry["event"] == "Command completed")
        assert completed["exit_code"] == 0
        assert completed["stdout_bytes"] == len("result")

    @pytest.mark.asyncio
    async def test_debug_log_of_the_command_masks_exported_values(self):
        executor = BashExecutor()

        with capture_logs() as logs:
            await executor.execute_bash_command("export TOKEN=s3cr3t; printf 'result'")

        command_log = next(entry for entry in logs if entry["event"] == "Bash command")
        assert command_log["log_level"] == "debug"
        assert command_log["command"] == "export TOKEN=<redacted>; printf 'result'"

    @pytest.mark.asyncio
    async def test_timeout_does_not_wait_for_detached_descendant_holding_pipes(self, tmp_path):
        executor = BashExecutor()
        child_pid_path = tmp_path / "detached-child.pid"
        execution_task = asyncio.create_task(
            executor.execute_bash_command(
                _detached_child_command(child_pid_path),
                timeout=0.5,
                graceful_termination_timeout=0,
            )
        )
        child_pid = None

        try:
            child_pid = await _wait_for_pid_file(child_pid_path)
            done, _ = await asyncio.wait({execution_task}, timeout=1.5)
            assert execution_task in done
            with pytest.raises(BashCommandExecutionTimeoutError):
                await execution_task
        finally:
            if child_pid is not None:
                _kill_process_if_running(child_pid)
            if not execution_task.done():
                execution_task.cancel()
            await asyncio.gather(execution_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_cancellation_does_not_wait_for_detached_descendant_holding_pipes(self, tmp_path):
        executor = BashExecutor()
        child_pid_path = tmp_path / "cancelled-detached-child.pid"
        execution_task = asyncio.create_task(
            executor.execute_bash_command(
                _detached_child_command(child_pid_path),
                timeout=10,
                graceful_termination_timeout=0,
            )
        )
        child_pid = None

        try:
            child_pid = await _wait_for_pid_file(child_pid_path)
            execution_task.cancel()
            done, _ = await asyncio.wait({execution_task}, timeout=1.0)
            assert execution_task in done
            with pytest.raises(asyncio.CancelledError):
                await execution_task
        finally:
            if child_pid is not None:
                _kill_process_if_running(child_pid)
            if not execution_task.done():
                execution_task.cancel()
            await asyncio.gather(execution_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_timeout_kills_same_group_child_that_ignores_sigterm(self, tmp_path):
        executor = BashExecutor()
        child_pid_path = tmp_path / "same-group-child.pid"
        child_script = "\n".join(
            [
                "import os, pathlib, signal, time",
                "child_pid = os.fork()",
                "if child_pid == 0:",
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                f"    pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid()))",
                "    time.sleep(10)",
                "else:",
                f"    while not pathlib.Path({str(child_pid_path)!r}).exists():",
                "        time.sleep(0.01)",
                "    time.sleep(10)",
            ]
        )
        child_pid = None

        try:
            with pytest.raises(BashCommandExecutionTimeoutError):
                await executor.execute_bash_command(
                    f"{shlex.quote(sys.executable)} -c {shlex.quote(child_script)}",
                    timeout=0.5,
                    graceful_termination_timeout=0.1,
                )

            child_pid = await _wait_for_pid_file(child_pid_path)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 1.0
            while _process_is_running(child_pid) and loop.time() < deadline:
                await asyncio.sleep(0.01)
            assert not _process_is_running(child_pid)
        finally:
            if child_pid is not None:
                _kill_process_if_running(child_pid)
