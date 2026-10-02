import asyncio
import contextlib
import os
import pwd
import re
import shlex
import shutil
import signal
import sys
import tempfile
from asyncio.subprocess import Process
from collections import deque
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Optional

from idegym.api.exceptions import IdeGYMException
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
# Resolved against the server's own PATH: the child runs with the caller's `env`, whose PATH
# need not contain them.
_BASH = shutil.which("bash") or "/bin/bash"
_RUNUSER = shutil.which("runuser") or "/usr/sbin/runuser"
_SUDO = shutil.which("sudo") or "/usr/bin/sudo"
# Evaluated under `bash -c` rather than run as `bash <file>`, so `$0` is `bash` and errors read
# `bash: line N:` instead of naming the temp file. The script is read from an inherited descriptor
# (see `_process_argv`), which is then closed so the command's children do not inherit it. The
# file always holds the init prefix, so an empty read is a failure.
_EVAL_SCRIPT_DESCRIPTOR = (
    'IFS= read -r -d "" -u {fd} __idegym_script || [ -n "$__idegym_script" ] || '
    '{{ echo "IdeGYM: cannot read the bash script" >&2 ; exit 1 ; }} ; '
    'exec {fd}<&- ; eval "unset __idegym_script ; $__idegym_script"'
)
# sudo closes every inherited descriptor above stderr, so the trampoline reopens the script here.
_SUDO_SCRIPT_DESCRIPTOR = 3
# Runs as root under `sudo`, so it can open the server user's 0600 files. sudo rewrites the
# environment (`secure_path` replaces PATH, `env_delete` drops LD_*, PYTHONPATH, BASH_ENV, ...), so
# the executor's environment travels in a file and is restored verbatim for `runuser`.
_SUDO_TRAMPOLINE = f"""
import os, sys
script, environment, *argv = sys.argv[1:]
os.dup2(os.open(script, os.O_RDONLY), {_SUDO_SCRIPT_DESCRIPTOR})
os.set_inheritable({_SUDO_SCRIPT_DESCRIPTOR}, True)
with open(environment, "rb") as handle:
    entries = handle.read().split(b"\\0")
os.execve(argv[0], argv, dict(entry.split(b"=", 1) for entry in entries if entry))
"""


class BashExecutorError(IdeGYMException):
    pass


class BashCommandExecutionTimeoutError(BashExecutorError):
    pass


class BashExecutorRequestError(BashExecutorError):
    """Caller-supplied context the executor cannot honour; the server maps the whole family to 400."""


class BashExecutorUnknownUserError(BashExecutorRequestError):
    """The requested ``user`` does not exist in the container."""


class BashExecutorWorkingDirectoryError(BashExecutorRequestError):
    """The requested ``cwd`` does not exist or is not a directory."""


class BashExecutorUserSwitchError(BashExecutorRequestError):
    """The server can neither ``runuser`` (it is not root) nor ``sudo`` without a password."""


class _UserSwitch(StrEnum):
    RUNUSER = "runuser"
    SUDO = "sudo"


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
    """Decode one stream, replacing undecodable bytes so binary output cannot fail the request.

    Whitespace is kept unless ``strip`` is set: a trailing newline is part of a ``git diff``.
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
    """Mask the values of ``export NAME=...`` assignments before a script is logged.

    Callers routinely ship credentials in scripts; the name is kept so the log line stays useful.
    """
    return _EXPORT_ASSIGNMENT_PATTERN.sub(lambda match: f"export {match.group('name')}=<redacted>", command)


def _command_excerpt(command: str) -> str:
    return _log_excerpt(_redact_exports(command))


def _prepend_bash_integration(command: str) -> str:
    """Prefix the caller's script with the bundled bash-integration init, on the same line.

    Joined with ``;`` rather than ``&&`` (which would make only the first statement conditional)
    so the script keeps its own semantics and line numbers. A missing init still fails fast; the
    guard tests readability, not the status of ``source``, which is that of the last command in
    the sourced ``~/.bashrc``.
    """
    init = shlex.quote(str(__BASH_INIT_FILEPATH__))
    guard = f'[ -r {init} ] || {{ echo "IdeGYM: cannot read the bash integration at {init}" >&2 ; exit 1 ; }}'
    return f"{guard} ; source {init} ; {command}"


def _user_environment(user: str) -> dict[str, str]:
    """Identity variables for ``user``, which ``runuser --preserve-environment`` leaves as root's.

    The init sources ``~/.bashrc``, so without them the script would load root's configuration and
    miss tools installed in the user's home (SDKMAN, pyenv, nvm).
    """
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        raise BashExecutorUnknownUserError(f"No such user in this container: {user}") from None
    return {"HOME": entry.pw_dir, "USER": user, "LOGNAME": user, "SHELL": entry.pw_shell or "/bin/bash"}


def _process_argv(descriptor: int, user: Optional[str]) -> list[str]:
    """Build the argv that runs the script held open on ``descriptor``, optionally as ``user``.

    ``runuser`` rather than ``su`` because it does not authenticate and keeps the environment.
    The script goes by descriptor, opened before the privilege drop, so the file can stay 0600 and
    owned by the server's user; reopening it by path (even ``/dev/fd/N``) is checked against the
    target user and refused.
    """
    invocation = [_BASH, "-c", _EVAL_SCRIPT_DESCRIPTOR.format(fd=descriptor), "bash"]
    if user is None:
        return invocation
    return [_RUNUSER, "--preserve-environment", "-u", user, "--", *invocation]


def _sudo_argv(script_path: str, environment_path: str, user: str) -> list[str]:
    """Build the argv that runs as ``user`` via ``sudo`` and ``_SUDO_TRAMPOLINE`` on a non-root server."""
    return _root_python_argv(
        _SUDO_TRAMPOLINE, script_path, environment_path, *_process_argv(_SUDO_SCRIPT_DESCRIPTOR, user)
    )


def _root_python_argv(code: str, *arguments: str) -> list[str]:
    """Run ``code`` as root through passwordless sudo, isolated from the environment sudo leaves."""
    return [_SUDO, "-n", "--", sys.executable, "-I", "-S", "-c", code, *arguments]


async def _succeeds(argv: list[str]) -> bool:
    """Run a helper command without any I/O and report whether it exited 0."""
    try:
        helper = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    return await helper.wait() == 0


def _create_script_file() -> tuple[int, str]:
    """Create a private (0600) temp file for the script and return its descriptor and path.

    A file avoids the 128 KiB ``MAX_ARG_STRLEN`` cap on a ``bash -c`` argument and, unlike stdin,
    cannot be swallowed by a command in the script that reads stdin. It runs on the event loop,
    not in a thread, so a cancellation cannot lose the path before cleanup removes it.
    """
    return tempfile.mkstemp(prefix="idegym-bash-", suffix=".sh")


def _write_script(descriptor: int, script: str) -> None:
    """Write the script through ``descriptor``, leaving it open and rewound for the child to read."""
    with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
        handle.write(script)
    os.lseek(descriptor, 0, os.SEEK_SET)


def _write_environment(descriptor: int, environment: dict[str, str]) -> None:
    """Write ``environment`` as NUL-separated ``NAME=value`` entries for the sudo trampoline."""
    entries = b"".join(os.fsencode(name) + b"=" + os.fsencode(value) + b"\0" for name, value in environment.items())
    with os.fdopen(descriptor, "wb", closefd=False) as handle:
        handle.write(entries)


def _write_files(
    script_descriptor: int,
    script: str,
    environment_descriptor: Optional[int],
    environment: dict[str, str],
) -> None:
    _write_script(script_descriptor, script)
    if environment_descriptor is not None:
        _write_environment(environment_descriptor, environment)


def _discard_script(descriptor: int, path: str) -> None:
    with contextlib.suppress(OSError):
        os.close(descriptor)
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


async def _sudo_signal_process_group(process_group_id: int, requested_signal: signal.Signals) -> bool:
    """Signal a process group as root, since under ``sudo`` its members belong to other users.

    A plain ``killpg`` reaches only ``sudo``, which relays to its direct child alone.
    """
    return await _succeeds(
        _root_python_argv(
            "import os, sys; os.killpg(int(sys.argv[1]), int(sys.argv[2]))",
            str(process_group_id),
            str(int(requested_signal)),
        )
    )


async def _signal_group(process: Process, requested_signal: signal.Signals, through_sudo: bool) -> bool:
    signalled = _signal_process_group(process, requested_signal)
    if through_sudo:
        signalled = await _sudo_signal_process_group(process.pid, requested_signal) or signalled
    return signalled


async def terminate_process_group(
    process: Process, graceful_termination_timeout: float = 2.0, through_sudo: bool = False
) -> None:
    if not await _signal_group(process, signal.SIGTERM, through_sudo):
        logger.info(f"Process group {process.pid} was already terminated")
        return

    if await _wait_for_process_group_exit(process.pid, graceful_termination_timeout):
        logger.info(f"Process group {process.pid} terminated gracefully")
        return

    if await _signal_group(process, signal.SIGKILL, through_sudo):
        logger.info(f"Process group {process.pid} was forcefully killed")
    else:
        logger.info(f"Process group {process.pid} was already terminated")


class BashExecutor:
    def __init__(self, working_directory: Optional[Path] = None):
        self.working_directory = working_directory
        self._sudo_available: Optional[bool] = None

    async def _can_sudo(self) -> bool:
        """Whether the trampoline can run through ``sudo`` without a password; checked once."""
        if self._sudo_available is None:
            self._sudo_available = await _succeeds(_root_python_argv(""))
        return self._sudo_available

    async def resolve_user_switch(self, user: Optional[str]) -> Optional[_UserSwitch]:
        """Decide how to run as ``user``, or return ``None`` for no user or the server's own.

        Root uses ``runuser``; a non-root server (the server image) uses passwordless ``sudo``.
        If neither works the request is rejected here, not as an ordinary exit 1 from the child.
        """
        if user is None:
            return None
        try:
            entry = pwd.getpwnam(user)
        except KeyError:
            raise BashExecutorUnknownUserError(f"No such user in this container: {user}") from None
        if entry.pw_uid == os.geteuid():
            return None
        if os.geteuid() == 0:
            return _UserSwitch.RUNUSER
        if await self._can_sudo():
            return _UserSwitch.SUDO
        raise BashExecutorUserSwitchError(
            f"Cannot run as {user}: the server is neither root nor allowed passwordless sudo"
        )

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
        """Run ``command`` under the bash-integration init and return ``(stdout, stderr, exit_code)``.

        The child gets a cleaned environment and its own process group, which is killed on
        timeout with ``BashCommandExecutionTimeoutError``. ``cwd``, ``env`` and ``user`` set
        per-command context outside the script text, so ``env`` values are never logged; ``user``
        needs root or passwordless sudo. Output is verbatim unless ``strip_output`` is set.
        """
        stdout_collector = _OutputCollector(max_output_bytes)
        stderr_collector = _OutputCollector(max_output_bytes)
        working_directory = self.resolve_working_directory(cwd)
        switch = await self.resolve_user_switch(user)
        # The user's identity goes on before the caller's env, so an explicit HOME still wins.
        environment = cleanenv() | (_user_environment(user) if user is not None else {}) | (env or {})
        logger.info(
            "Executing bash command",
            command_chars=len(command),
            cwd=str(working_directory) if working_directory else None,
            env_names=sorted(env) if env else [],
            user=user,
            user_switch=switch,
        )
        logger.debug("Bash command", command=_command_excerpt(command))

        bash_command = _prepend_bash_integration(command)
        through_sudo = switch is _UserSwitch.SUDO
        descriptor, script_path = _create_script_file()
        environment_descriptor: Optional[int] = None
        environment_path: Optional[str] = None
        # One try/finally, so a cancellation at any await still reaches cleanup. The write is
        # shielded and awaited there because its thread keeps using the descriptors after a
        # cancelled await returns.
        write: Optional[asyncio.Future[None]] = None
        try:
            if through_sudo:
                environment_descriptor, environment_path = _create_script_file()
            write = asyncio.ensure_future(
                asyncio.to_thread(_write_files, descriptor, bash_command, environment_descriptor, environment)
            )
            await asyncio.shield(write)
            if through_sudo:
                argv, inherited = _sudo_argv(script_path, environment_path, user), ()
            else:
                argv, inherited = _process_argv(descriptor, None if switch is None else user), (descriptor,)
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
                preexec_fn=os.setsid,
                env=environment,
                pass_fds=inherited,
            )

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
                    await terminate_process_group(process, graceful_termination_timeout, through_sudo)
                except asyncio.CancelledError:
                    termination_started = True
                    await terminate_process_group(process, graceful_termination_timeout, through_sudo)
                    raise
            finally:
                if not termination_started and (process.returncode is None or not communication_task.done()):
                    await terminate_process_group(process, graceful_termination_timeout, through_sudo)
                await _finish_output_drain(process, communication_task)
                _close_output_pipes(process)
                await _reap_process(process)
        finally:
            if write is not None:
                await asyncio.gather(write, return_exceptions=True)
            _discard_script(descriptor, script_path)
            if environment_descriptor is not None:
                _discard_script(environment_descriptor, environment_path)

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
