import asyncio
import contextlib
import os
import pwd
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import psutil
import pytest
from idegym.backend.utils import bash_executor as bash_executor_module
from idegym.backend.utils.bash_executor import (
    BashCommandExecutionTimeoutError,
    BashExecutor,
    BashExecutorUserSwitchError,
)
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


def _bash_version() -> tuple[int, ...]:
    output = subprocess.run(
        [bash_executor_module._BASH, "-c", 'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return tuple(int(part) for part in output.split("."))


# Under `bash -c`, and `eval` within it, bash before 5.1 miscounts script lines in its error
# messages, and 3.2 (macOS) omits `line N` for a one-line script. That is bash's own behaviour,
# not the executor's; the server image ships 5.2.
_needs_bash_5_1_error_format = pytest.mark.skipif(
    _bash_version() < (5, 1), reason="bash < 5.1 formats `bash -c` error locations differently"
)


def _uid_of(name: str) -> Optional[int]:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


# `devuser` and `appuser` exist only in the docker test image, which mirrors the server image.
_SWITCH_TARGET = "devuser"
_needs_switch_target = pytest.mark.skipif(
    _uid_of(_SWITCH_TARGET) in (None, os.geteuid()),
    reason="needs the docker test image's devuser, as a different user",
)


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
    async def test_empty_command_is_a_no_op(self):
        """An empty script runs the init prefix and nothing else, rather than failing to parse."""
        executor = BashExecutor()
        stdout, stderr, exit_code = await executor.execute_bash_command("")

        assert stdout == ""
        assert stderr == ""
        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_every_statement_runs_not_just_the_first(self):
        """`source ... && a; b; c` used to make only `a` conditional; all three must run now."""
        executor = BashExecutor()

        stdout, _stderr, exit_code = await executor.execute_bash_command(
            "printf a; printf b; printf c", strip_output=True
        )

        assert (stdout, exit_code) == ("abc", 0)

    @pytest.mark.asyncio
    async def test_a_failing_first_statement_still_short_circuits_the_callers_own_chain(self):
        """The caller's own `&&` must keep working — only the injected one is gone."""
        executor = BashExecutor()

        stdout, _stderr, exit_code = await executor.execute_bash_command("false && printf 'unreachable'")

        assert stdout == ""
        assert exit_code != 0

    @_needs_bash_5_1_error_format
    @pytest.mark.asyncio
    async def test_bash_reports_errors_at_the_callers_own_line_numbers(self):
        executor = BashExecutor()

        _stdout, stderr, _exit_code = await executor.execute_bash_command("true\ntrue\nthis-command-does-not-exist")

        assert stderr.startswith("bash: line 3: this-command-does-not-exist:")

    @_needs_bash_5_1_error_format
    @pytest.mark.asyncio
    async def test_errors_carry_the_bash_c_prefix_not_the_temp_file_name(self):
        executor = BashExecutor()

        _stdout, stderr, exit_code = await executor.execute_bash_command("nosuchcmd")

        assert stderr.startswith("bash: line 1: nosuchcmd:")
        assert exit_code == 127

    @pytest.mark.asyncio
    async def test_dollar_zero_is_bash_and_there_are_no_positional_parameters(self):
        executor = BashExecutor()

        stdout, _stderr, exit_code = await executor.execute_bash_command('printf "%s %s" "$0" "$#"')

        assert (stdout, exit_code) == ("bash 0", 0)

    @pytest.mark.asyncio
    async def test_a_bashrc_ending_in_a_failing_command_does_not_abort_the_script(self, tmp_path):
        """`source` returns the rc file's last status; a trailing `[ -f ... ] && ...` is routine."""
        (tmp_path / ".bashrc").write_text("export FROM_BASHRC=1\n[ -f ~/.not-installed ] && source ~/.not-installed\n")
        executor = BashExecutor()

        stdout, stderr, exit_code = await executor.execute_bash_command(
            'printf "%s" "$FROM_BASHRC"', env={"HOME": str(tmp_path)}
        )

        assert (stdout, stderr, exit_code) == ("1", "", 0)

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
        stdout, _stderr, exit_code = await executor.execute_bash_command("printf '\\377\\376'; exit 3")

        # Each invalid byte becomes its own U+FFFD, so two bytes give two replacement characters.
        assert stdout == "��"
        assert exit_code == 3

    @pytest.mark.asyncio
    async def test_cwd_overrides_the_executor_directory_for_one_command(self, tmp_path):
        nested = tmp_path / "nested"
        nested.mkdir()
        executor = BashExecutor(working_directory=tmp_path)

        relative, _stderr, _code = await executor.execute_bash_command("pwd", cwd="nested", strip_output=True)
        absolute, _stderr, _code = await executor.execute_bash_command("pwd", cwd=str(nested), strip_output=True)
        default, _stderr, _code = await executor.execute_bash_command("pwd", strip_output=True)

        assert Path(relative).resolve() == nested.resolve()
        assert Path(absolute).resolve() == nested.resolve()
        assert Path(default).resolve() == tmp_path.resolve()

    @pytest.mark.asyncio
    async def test_env_reaches_the_command_without_entering_the_script(self):
        executor = BashExecutor()

        with capture_logs() as logs:
            stdout, _stderr, exit_code = await executor.execute_bash_command(
                'printf "%s" "$SECRET_TOKEN"', env={"SECRET_TOKEN": "s3cr3t"}
            )

        assert (stdout, exit_code) == ("s3cr3t", 0)
        command_log = next(entry for entry in logs if entry["event"] == "Bash command")
        assert "s3cr3t" not in command_log["command"]
        start_log = next(entry for entry in logs if entry["event"] == "Executing bash command")
        assert start_log["env_names"] == ["SECRET_TOKEN"]

    @pytest.mark.asyncio
    async def test_env_overrides_an_inherited_variable(self, monkeypatch):
        monkeypatch.setenv("IDEGYM_TEST_MARKER", "inherited")
        executor = BashExecutor()

        stdout, _stderr, _code = await executor.execute_bash_command(
            'printf "%s" "$IDEGYM_TEST_MARKER"', env={"IDEGYM_TEST_MARKER": "overridden"}
        )

        assert stdout == "overridden"

    @pytest.mark.asyncio
    async def test_a_callers_path_does_not_affect_finding_bash(self, tmp_path):
        executor = BashExecutor()

        stdout, _stderr, exit_code = await executor.execute_bash_command(
            'printf "%s" "$PATH"', env={"PATH": "/nonexistent/bin", "HOME": str(tmp_path)}
        )

        assert (stdout, exit_code) == ("/nonexistent/bin", 0)

    def test_the_sudo_trampoline_restores_the_environment_and_the_script_descriptor(self, tmp_path):
        """Run the trampoline without sudo: it must hand on fd 3 and exactly the environment written."""
        script = tmp_path / "script"
        script.write_text("from-descriptor")
        environment_file = tmp_path / "environment"
        descriptor = os.open(environment_file, os.O_WRONLY | os.O_CREAT, 0o600)
        environment = {"PYTHONPATH": "/p", "WITH_EQUALS": "a=b", "EMPTY": ""}
        try:
            bash_executor_module._write_environment(descriptor, environment)
        finally:
            os.close(descriptor)

        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                bash_executor_module._SUDO_TRAMPOLINE,
                str(script),
                str(environment_file),
                bash_executor_module._BASH,
                "-c",
                'IFS= read -r -d "" -u 3 body; printf "%s|%s|%s|%s|%s" "$body" "$PYTHONPATH" "$WITH_EQUALS" "${EMPTY-unset}" "${UNRELATED-gone}"',
            ],
            capture_output=True,
            text=True,
            env={"UNRELATED": "dropped"},
        )

        assert result.stdout == "from-descriptor|/p|a=b||gone"

    @_needs_switch_target
    @pytest.mark.asyncio
    async def test_a_user_switch_keeps_the_script_private_and_the_environment_intact(self):
        """Covers runuser when run as root and the sudo trampoline when run as `appuser`.

        The script must stay 0600, and PATH, LD_* and PYTHONPATH must survive sudo's rewriting.
        """
        executor = BashExecutor()
        glob = shlex.quote(tempfile.gettempdir()) + "/idegym-bash-*"
        script = (
            'id -un; printf "%s\\n" "$HOME" "$PATH" "$PYTHONPATH" "$LD_LIBRARY_PATH"\n'
            f'for f in {glob}; do [ -r "$f" ] && echo "readable: $f"; done; true'
        )

        stdout, stderr, exit_code = await executor.execute_bash_command(
            script,
            user=_SWITCH_TARGET,
            env={"PATH": "/custom/bin:/usr/bin:/bin", "PYTHONPATH": "/p", "LD_LIBRARY_PATH": "/l"},
        )

        assert (stdout, stderr, exit_code) == (
            f"{_SWITCH_TARGET}\n/home/{_SWITCH_TARGET}\n/custom/bin:/usr/bin:/bin\n/p\n/l\n",
            "",
            0,
        )

    @_needs_switch_target
    @pytest.mark.asyncio
    async def test_a_timeout_under_a_user_switch_still_returns(self):
        """Under sudo the server may not signal the target user's processes; none may survive."""
        executor = BashExecutor()
        loop = asyncio.get_running_loop()
        started = loop.time()

        with pytest.raises(BashCommandExecutionTimeoutError):
            await executor.execute_bash_command(
                "sleep 31 & sleep 31", user=_SWITCH_TARGET, timeout=0.5, graceful_termination_timeout=0.5
            )

        assert loop.time() - started < 10
        deadline = loop.time() + 2
        while survivors := [
            process.pid
            for process in psutil.process_iter(["username", "cmdline"])
            if process.info["username"] == _SWITCH_TARGET and process.info["cmdline"] == ["sleep", "31"]
        ]:
            assert loop.time() < deadline, f"processes outlived the timeout: {survivors}"
            await asyncio.sleep(0.05)

    @pytest.mark.skipif(
        pwd.getpwuid(os.geteuid()).pw_name != _SWITCH_TARGET,
        reason="needs to run as the docker test image's devuser, which is neither root nor a sudoer",
    )
    @pytest.mark.asyncio
    async def test_a_user_switch_without_root_or_sudo_is_rejected(self):
        executor = BashExecutor()

        with pytest.raises(BashExecutorUserSwitchError, match="passwordless sudo"):
            await executor.execute_bash_command("true", user="appuser")

    @pytest.mark.asyncio
    async def test_the_servers_own_user_needs_no_switch(self):
        """The server image runs as appuser, so `user="appuser"` must work without root or sudo."""
        executor = BashExecutor()
        own_user = pwd.getpwuid(os.geteuid()).pw_name

        stdout, _stderr, exit_code = await executor.execute_bash_command("id -un", user=own_user, strip_output=True)

        assert (stdout, exit_code) == (own_user, 0)

    @pytest.mark.asyncio
    async def test_a_script_far_larger_than_the_argument_limit_runs(self):
        """MAX_ARG_STRLEN is 128 KiB; this script is well past it and used to fail with E2BIG."""
        executor = BashExecutor()
        payload = "x" * (512 * 1024)
        script = f'PAYLOAD={payload}\nprintf "%s" "${{#PAYLOAD}}"'

        stdout, stderr, exit_code = await executor.execute_bash_command(script)

        assert (stdout, exit_code) == (str(len(payload)), 0)
        assert stderr == ""

    @pytest.mark.asyncio
    async def test_the_script_file_is_removed_after_the_command(self, monkeypatch):
        executor = BashExecutor()
        written: list[str] = []
        original = bash_executor_module._create_script_file

        def record():
            descriptor, path = original()
            written.append(path)
            return descriptor, path

        monkeypatch.setattr(bash_executor_module, "_create_script_file", record)

        await executor.execute_bash_command("true")

        assert written and not Path(written[0]).exists()

    @pytest.mark.asyncio
    async def test_the_script_file_is_removed_after_a_timeout(self, monkeypatch):
        executor = BashExecutor()
        written: list[str] = []
        original = bash_executor_module._create_script_file

        def record():
            descriptor, path = original()
            written.append(path)
            return descriptor, path

        monkeypatch.setattr(bash_executor_module, "_create_script_file", record)

        with pytest.raises(BashCommandExecutionTimeoutError):
            await executor.execute_bash_command("sleep 10", timeout=0.2, graceful_termination_timeout=0.1)

        assert written and not Path(written[0]).exists()

    @pytest.mark.asyncio
    async def test_a_command_reading_stdin_does_not_consume_the_rest_of_the_script(self):
        """A script fed to bash on stdin would be eaten by `cat`; running it from a file is safe."""
        executor = BashExecutor()

        stdout, _stderr, exit_code = await executor.execute_bash_command("cat > /dev/null\nprintf 'still here'")

        assert (stdout, exit_code) == ("still here", 0)

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
