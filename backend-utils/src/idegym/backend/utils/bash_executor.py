import asyncio
import contextlib
import os
import pwd
import re
import shlex
import signal
import tempfile
from asyncio.subprocess import Process
from collections import deque
from importlib.resources import files
from pathlib import Path
from typing import Optional

from idegym.backend import resources
from idegym.backend.utils.environment import cleanenv
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

__BASH_INIT_FILEPATH__ = files(resources).joinpath("bash-integration.bash")
_READ_CHUNK_BYTES = 64 * 1024
_LOG_EXCERPT_CHARS = 1800
_OUTPUT_DRAIN_TIMEOUT_SECONDS = 0.25
_PROCESS_GROUP_POLL_INTERVAL_SECONDS = 0.01
_PROCESS_REAP_TIMEOUT_SECONDS = 0.25
_EXPORT_ASSIGNMENT_PATTERN = re.compile(
    r"""\bexport[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?:'[^']*'|"[^"]*"|[^\s;&|)]*)"""
)


class BashExecutorError(Exception):
    pass


class BashCommandExecutionTimeoutError(BashExecutorError):
    pass


class BashExecutorUnknownUserError(BashExecutorError):
    """The requested ``user`` does not exist in the container."""


class BashExecutorWorkingDirectoryError(BashExecutorError):
    """The requested ``cwd`` does not exist or is not a directory."""


class _OutputCollector:
    """Retain complete output or a bounded head and chunk-ring tail while tracking total bytes."""

    def __init__(self, max_bytes: Optional[int]):
        if max_bytes is not None:
            if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
                raise TypeError("max_output_bytes must be an integer or None")
            if max_bytes <= 0:
                raise ValueError("max_output_bytes must be positive or None")
        self.max_bytes = max_bytes
        self.total = 0
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_bytes = 0
        self._chunks: list[bytes] = []

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.max_bytes is None:
            self._chunks.append(chunk)
            return

        head_limit = self.max_bytes // 2
        tail_limit = self.max_bytes - head_limit
        head_space = head_limit - len(self._head)
        if head_space > 0:
            self._head.extend(chunk[:head_space])
            chunk = chunk[head_space:]
        if not chunk or tail_limit == 0:
            return

        self._tail.append(chunk)
        self._tail_bytes += len(chunk)
        overflow = self._tail_bytes - tail_limit
        while overflow > 0:
            first = self._tail[0]
            if len(first) <= overflow:
                self._tail.popleft()
                self._tail_bytes -= len(first)
                overflow -= len(first)
            else:
                self._tail[0] = first[overflow:]
                self._tail_bytes -= overflow
                overflow = 0

    def retained(self) -> bytes:
        if self.max_bytes is None:
            return b"".join(self._chunks)
        tail = b"".join(self._tail)
        if self.total <= self.max_bytes:
            return bytes(self._head) + tail
        omitted = self.total - len(self._head) - len(tail)
        marker = f"\n... [IdeGYM truncated {omitted} output bytes] ...\n".encode()
        return bytes(self._head) + marker + tail


async def _drain_output(stream: asyncio.StreamReader, collector: _OutputCollector) -> None:
    while chunk := await stream.read(_READ_CHUNK_BYTES):
        collector.append(chunk)


async def _read_bounded(stream: asyncio.StreamReader, max_bytes: Optional[int]) -> tuple[bytes, int]:
    """Drain one stream and return retained output plus its unbounded byte count."""
    collector = _OutputCollector(max_bytes)
    await _drain_output(stream, collector)
    return collector.retained(), collector.total


async def _communicate_bounded(
    process: Process,
    stdout_collector: _OutputCollector,
    stderr_collector: _OutputCollector,
) -> None:
    """Drain both pipes to EOF, which lets asyncio close their transports, and reap the process."""
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Subprocess output pipes are unavailable")
    await asyncio.gather(
        _drain_output(process.stdout, stdout_collector),
        _drain_output(process.stderr, stderr_collector),
        process.wait(),
    )


def _close_output_pipes(process: Process) -> None:
    """Close subprocess read transports when a detached descendant prevents pipe EOF."""
    process_transport = getattr(process, "_transport", None)
    if process_transport is None:
        return
    for file_descriptor in (1, 2):
        pipe_transport = process_transport.get_pipe_transport(file_descriptor)
        if pipe_transport is not None:
            pipe_transport.close()


async def _finish_output_drain(process: Process, communication_task: asyncio.Task[None]) -> None:
    """Give shutdown output a bounded drain window, then close and cancel lingering readers."""
    if not communication_task.done():
        done, _ = await asyncio.wait({communication_task}, timeout=_OUTPUT_DRAIN_TIMEOUT_SECONDS)
        if not done:
            _close_output_pipes(process)
            communication_task.cancel()
    await asyncio.gather(communication_task, return_exceptions=True)


async def _reap_process(process: Process) -> None:
    """Bound the final wait in case process-group signaling failed unexpectedly."""
    if process.returncode is not None:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=_PROCESS_REAP_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning(
            "Process did not exit after termination",
            process_id=process.pid,
            timeout_seconds=_PROCESS_REAP_TIMEOUT_SECONDS,
        )


def _decode_output(output: bytes, strip: bool = False) -> str:
    """Decode one stream, replacing undecodable bytes so a binary write cannot fail the request.

    Output is returned byte-for-byte otherwise: a trailing newline is part of a ``git diff`` and
    ``printf 'x'`` must stay distinguishable from ``printf '  x  '``. ``strip`` is the opt-in for
    callers that would otherwise trim the result themselves.
    """
    if not output:
        return ""
    text = output.decode("utf-8", errors="replace")
    return text.strip() if strip else text


def _log_excerpt(text: str) -> str:
    if len(text) <= _LOG_EXCERPT_CHARS:
        return text
    half = _LOG_EXCERPT_CHARS // 2
    return f"{text[:half]}\n... [log excerpt truncated] ...\n{text[-half:]}"


def _redact_exports(command: str) -> str:
    """Mask the values of ``export NAME=...`` assignments in a script before it is logged.

    A script is the only channel for setting per-command environment, so callers routinely
    ship credentials inside it. The variable name is kept because it is what makes a log line
    useful; only the value goes.
    """
    return _EXPORT_ASSIGNMENT_PATTERN.sub(lambda match: f"export {match.group('name')}=<redacted>", command)


def _command_excerpt(command: str) -> str:
    return _log_excerpt(_redact_exports(command))


def _prepend_bash_integration(command: str) -> str:
    """Prefix the caller's script with the bundled bash-integration init.

    The two are joined with ``;`` rather than ``&&``. With ``&&`` only the script's *first*
    statement was conditional on the init succeeding and the rest ran regardless, so
    ``a; b; c`` did not mean what the caller wrote — clients defended by wrapping every script
    in a brace group. Keeping the prefix on the same line also keeps the caller's line numbers
    intact, so a bash error still points at the right line of their script.
    """
    init = shlex.quote(str(__BASH_INIT_FILEPATH__))
    # `;` rather than `&&` so the caller's script keeps its own semantics, but the init is still
    # guarded: without this a missing or unreadable init left the script running in an
    # unconfigured shell and failing later as "command not found", with the caller's exit code.
    guard = f'source {init} || {{ echo "IdeGYM: failed to source the bash integration at {init}" >&2 ; exit 1 ; }}'
    return f"{guard} ; {command}"


def _user_environment(user: str) -> dict[str, str]:
    """Identity variables for ``user``, which ``runuser --preserve-environment`` does not set.

    ``-p`` keeps the whole environment on purpose — that is how the caller's ``env`` and the
    cleaned server environment survive — but it therefore also keeps *root's* ``HOME``. The
    bundled init sources ``~/.bashrc``, so without these the script would load root's shell
    configuration and miss anything installed in the target user's home (SDKMAN, pyenv, nvm),
    while writes to ``~`` would land in a directory the user cannot write.
    """
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        raise BashExecutorUnknownUserError(f"No such user in this container: {user}") from None
    return {"HOME": entry.pw_dir, "USER": user, "LOGNAME": user, "SHELL": entry.pw_shell or "/bin/bash"}


def _process_argv(script_path: str, user: Optional[str]) -> list[str]:
    """Build the argv that runs the script file, optionally dropping to another user.

    ``runuser`` is used rather than ``su`` because it does not authenticate and keeps the
    caller's environment, which is what the ``env`` argument has already been merged into.
    """
    invocation = ["bash", script_path]
    if user is None:
        return invocation
    return ["runuser", "--preserve-environment", "-u", user, "--", *invocation]


def _write_script(script: str, readable_by_other_user: bool) -> str:
    """Write the script to a temp file and return its path.

    Passing the script as a ``bash -c`` argument capped it at Linux's ``MAX_ARG_STRLEN``
    (128 KiB), and an oversized script failed with a bare ``E2BIG`` rather than anything a
    caller could act on. A file has no such ceiling, and unlike feeding bash on stdin it leaves
    the command's own stdin alone — a script read from stdin is consumed incrementally, so any
    command inside it that reads stdin would swallow the rest of the script.
    """
    descriptor, path = tempfile.mkstemp(prefix="idegym-bash-", suffix=".sh")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(script)
        if readable_by_other_user:
            # mkstemp creates 0600, which the target user of `runuser` could not read.
            # TODO: 0644 also exposes the script to any co-tenant process for the duration of the
            # run, which matters because callers do put credentials in scripts (see
            # `_redact_exports`). Prefer `os.chown(path, uid, -1)` with 0600, or a directory only
            # the target user can traverse.
            os.chmod(path, 0o644)
    except BaseException:
        _remove_script(path)
        raise
    return path


def _remove_script(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)


def _signal_process_group(process: Process, requested_signal: signal.Signals) -> bool:
    """Signal a process group, tolerating races after its leader has exited."""
    try:
        os.killpg(process.pid, requested_signal)
    except ProcessLookupError:
        return False
    except OSError as group_error:
        if process.returncode is not None:
            return False
        try:
            process.send_signal(requested_signal)
        except ProcessLookupError:
            return False
        except OSError:
            raise group_error
        return True
    return True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_process_group_exit(process_group_id: int, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout, 0)
    while _process_group_exists(process_group_id):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_PROCESS_GROUP_POLL_INTERVAL_SECONDS, remaining))
    return True


async def terminate_process_group(process: Process, graceful_termination_timeout: float = 2.0) -> None:
    if not _signal_process_group(process, signal.SIGTERM):
        logger.info(f"Process group {process.pid} was already terminated")
        return

    if await _wait_for_process_group_exit(process.pid, graceful_termination_timeout):
        logger.info(f"Process group {process.pid} terminated gracefully")
        return

    if _signal_process_group(process, signal.SIGKILL):
        logger.info(f"Process group {process.pid} was forcefully killed")
    else:
        logger.info(f"Process group {process.pid} was already terminated")


class BashExecutor:
    def __init__(self, working_directory: Optional[Path] = None):
        self.working_directory = working_directory

    def resolve_working_directory(self, cwd: Optional[str]) -> Optional[Path]:
        """Resolve a per-command ``cwd`` against the executor's directory.

        An absolute path is used as given; a relative one is taken from the executor's working
        directory, which is the server's project root.
        """
        if cwd is None:
            return self.working_directory
        requested = Path(cwd)
        if not requested.is_absolute() and self.working_directory is not None:
            requested = self.working_directory / requested
        # `cwd` is caller-supplied, so check it here: otherwise the child's chdir fails and
        # asyncio raises a bare FileNotFoundError that the router turns into a 500.
        if not requested.is_dir():
            raise BashExecutorWorkingDirectoryError(
                f"Working directory does not exist or is not a directory: {requested}"
            )
        return requested

    async def execute_bash_command(
        self,
        command: str,
        timeout: Optional[float] = 600.0,
        graceful_termination_timeout: float = 2.0,
        max_output_bytes: Optional[int] = None,
        strip_output: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        user: Optional[str] = None,
    ) -> tuple[str, str, int]:
        """
        Execute a bash command asynchronously.

        The command runs inside a bash-integration environment (sourced from a
        bundled init script) in a clean subprocess environment with IdeGYM-specific
        variables stripped. The process is started in its own process group so the
        entire group can be killed on timeout.

        ``cwd``, ``env`` and ``user`` give a caller per-command context without having to
        synthesize it into the script — an environment variable set through ``env`` never
        enters the command text, so it is not logged with it. ``user`` requires the executor
        to run as root, since it shells out through ``runuser``.

        Output is returned verbatim unless ``strip_output`` asks for surrounding
        whitespace to be trimmed. The script itself is written to a temp file and run as
        ``bash <file>``, so its size is not capped by the kernel's argument limit.

        Returns a tuple of (stdout, stderr, exit_code).
        Raises BashCommandExecutionTimeoutError if the timeout is exceeded.
        """
        stdout_collector = _OutputCollector(max_output_bytes)
        stderr_collector = _OutputCollector(max_output_bytes)
        working_directory = self.resolve_working_directory(cwd)
        # The user's identity goes on before the caller's env, so an explicit HOME still wins.
        environment = cleanenv() | (_user_environment(user) if user is not None else {}) | (env or {})
        logger.info(
            "Executing bash command",
            command_chars=len(command),
            cwd=str(working_directory) if working_directory else None,
            env_names=sorted(env) if env else [],
            user=user,
        )
        logger.debug("Bash command", command=_command_excerpt(command))

        bash_command = _prepend_bash_integration(command)
        script_path = await asyncio.to_thread(_write_script, bash_command, user is not None)
        try:
            process = await asyncio.create_subprocess_exec(
                *_process_argv(script_path, user),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
                preexec_fn=os.setsid,
                env=environment,
            )
        except BaseException:
            _remove_script(script_path)
            raise

        communication_task = asyncio.create_task(
            _communicate_bounded(process, stdout_collector, stderr_collector),
            name=f"bash-output-{process.pid}",
        )
        termination_started = False
        timed_out = False
        try:
            try:
                await asyncio.wait_for(asyncio.shield(communication_task), timeout=timeout)
            except TimeoutError:
                timed_out = True
                termination_started = True
                await terminate_process_group(process, graceful_termination_timeout)
            except asyncio.CancelledError:
                termination_started = True
                await terminate_process_group(process, graceful_termination_timeout)
                raise
        finally:
            if not termination_started and (process.returncode is None or not communication_task.done()):
                await terminate_process_group(process, graceful_termination_timeout)
            await _finish_output_drain(process, communication_task)
            _close_output_pipes(process)
            await _reap_process(process)
            _remove_script(script_path)

        if timed_out:
            logger.warning(
                "Command execution timed out",
                timeout_seconds=timeout,
                stdout_bytes=stdout_collector.total,
                stderr_bytes=stderr_collector.total,
            )
            logger.debug(
                "Partial output of the timed-out command",
                stdout=_log_excerpt(_decode_output(stdout_collector.retained())),
                stderr=_log_excerpt(_decode_output(stderr_collector.retained())),
            )
            raise BashCommandExecutionTimeoutError(f"Command execution timed out after {timeout} seconds")

        stdout_text = _decode_output(stdout_collector.retained(), strip=strip_output)
        stderr_text = _decode_output(stderr_collector.retained(), strip=strip_output)
        exit_code = process.returncode
        if exit_code is None:
            raise RuntimeError("Bash process completed without an exit code")

        logger.info(
            "Command completed",
            exit_code=exit_code,
            stdout_bytes=stdout_collector.total,
            stderr_bytes=stderr_collector.total,
        )
        logger.debug(
            "Command output",
            stdout=_log_excerpt(stdout_text),
            stderr=_log_excerpt(stderr_text),
        )

        return stdout_text, stderr_text, exit_code
