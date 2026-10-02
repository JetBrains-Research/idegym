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
# Resolved once, against the server's own PATH: the child is spawned with the caller's `env`,
# so a bare name would be looked up in a PATH the caller chose — `env={"PATH": "/opt/tool/bin"}`
# then failed with FileNotFoundError, which surfaced as a 404 rather than anything actionable.
_BASH = shutil.which("bash") or "/bin/bash"
_RUNUSER = shutil.which("runuser") or "/usr/sbin/runuser"
_SUDO = shutil.which("sudo") or "/usr/bin/sudo"
# `bash <file>` would set `$0` to the temp path and prefix every error with it
# (`/tmp/idegym-bash-k3j9x.sh: line 1: ...`, a different name per call), which broke stderr
# comparisons and made `$(dirname "$0")` resolve to /tmp. Evaluating the file's contents inside
# `bash -c` keeps what callers had before the script moved out of argv: `$0` is `bash` and errors
# read `bash: line N:`. The script arrives on an inherited descriptor rather than by path (see
# `_process_argv`), which is read and then closed so the command's own children do not inherit
# it. The file is never empty — the init prefix is in it — so an empty read is a failure.
_EVAL_SCRIPT_DESCRIPTOR = (
    'IFS= read -r -d "" -u {fd} __idegym_script || [ -n "$__idegym_script" ] || '
    '{{ echo "IdeGYM: cannot read the bash script" >&2 ; exit 1 ; }} ; '
    'exec {fd}<&- ; eval "unset __idegym_script ; $__idegym_script"'
)
# The descriptor number the sudo trampoline places the script on. sudo closes every inherited
# descriptor above stderr, so the trampoline has to reopen the file on the far side.
_SUDO_SCRIPT_DESCRIPTOR = 3
# Runs as root under `sudo`, so it can open the server user's private files. sudo also rewrites
# the environment it passes on — `secure_path` replaces PATH, and `env_delete` drops LD_*,
# PYTHONPATH, BASH_ENV and others — so the environment the executor built travels in a file
# and is restored verbatim for `runuser`.
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
    """Caller-supplied context the executor cannot honour: a bad request, not a server fault.

    The server maps this whole family to 400 in one handler, so every caller of the executor —
    the tools router, rewards, project reset — reports it the same way.
    """


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
    # The guard tests readability rather than the status of `source`, which is that of the last
    # command in the sourced chain — a `~/.bashrc` ending in `[ -f ~/.fzf.bash ] && ...` would
    # otherwise abort every command.
    guard = f'[ -r {init} ] || {{ echo "IdeGYM: cannot read the bash integration at {init}" >&2 ; exit 1 ; }}'
    return f"{guard} ; source {init} ; {command}"


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


def _process_argv(descriptor: int, user: Optional[str]) -> list[str]:
    """Build the argv that runs the script held open on ``descriptor``, optionally as ``user``.

    ``runuser`` is used rather than ``su`` because it does not authenticate and keeps the
    caller's environment, which is what the ``env`` argument has already been merged into.

    The script reaches bash as an inherited descriptor, not a path, so the file can stay 0600
    and owned by the server's user even when ``user`` is someone else: the descriptor was opened
    before the privilege drop, and ``runuser`` passes it through. Reopening it by path — even as
    ``/dev/fd/N`` — would be checked against the target user and refused, which is what made
    the file world-readable before. The alternative, ``chown`` to the target user, needs root on
    the server side and leaves a file in the sticky temp directory the server can no longer
    delete.
    """
    invocation = [_BASH, "-c", _EVAL_SCRIPT_DESCRIPTOR.format(fd=descriptor), "bash"]
    if user is None:
        return invocation
    return [_RUNUSER, "--preserve-environment", "-u", user, "--", *invocation]


def _sudo_argv(script_path: str, environment_path: str, user: str) -> list[str]:
    """Build the argv that reaches ``runuser`` through ``sudo`` when the server is not root.

    The server image runs as a non-root user with passwordless sudo, where ``runuser`` alone
    fails. sudo closes inherited descriptors and rewrites the environment, so rather than
    handing the target command either directly, it runs a root-side trampoline that reopens
    the script, restores the environment from ``environment_path``, and then execs the same
    ``runuser`` argv the root path uses.
    """
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

    Passing the script as a ``bash -c`` argument capped it at Linux's ``MAX_ARG_STRLEN``
    (128 KiB), and an oversized script failed with a bare ``E2BIG`` rather than anything a
    caller could act on. A file has no such ceiling, and unlike feeding bash on stdin it leaves
    the command's own stdin alone — a script read from stdin is consumed incrementally, so any
    command inside it that reads stdin would swallow the rest of the script.

    This runs on the event loop rather than in a worker thread on purpose: a request cancelled
    while ``mkstemp`` ran in a thread lost the path, and with it the only way to remove the file.
    """
    return tempfile.mkstemp(prefix="idegym-bash-", suffix=".sh")


def _write_script(descriptor: int, script: str) -> None:
    """Write the script through ``descriptor``, which stays open and owned by the caller.

    The offset is rewound because the child reads through the same open file description.
    """
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
    """Signal a process group through sudo, for members that run as another user.

    Under the sudo trampoline the group holds root's ``runuser`` and the target user's
    processes, none of which the server's own user may signal: a plain ``killpg`` reaches only
    ``sudo``, which relays to its direct child, and the command's own children outlived a
    timeout.
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
        """Decide how to run as ``user``, or return ``None`` when no switch is needed.

        Asking for the server's own user is not a switch. Otherwise root drops privileges with
        ``runuser`` directly; a non-root server goes through passwordless ``sudo``, which is
        how the server image is set up. A server that can do neither rejects the request up
        front — ``runuser`` would only have failed inside the child, as an ordinary exit 1.
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
        """
        Execute a bash command asynchronously.

        The command runs inside a bash-integration environment (sourced from a
        bundled init script) in a clean subprocess environment with IdeGYM-specific
        variables stripped. The process is started in its own process group so the
        entire group can be killed on timeout.

        ``cwd``, ``env`` and ``user`` give a caller per-command context without having to
        synthesize it into the script — an environment variable set through ``env`` never
        enters the command text, so it is not logged with it. ``user`` requires the executor
        to run as root or to have passwordless sudo; see ``resolve_user_switch``.

        Output is returned verbatim unless ``strip_output`` asks for surrounding
        whitespace to be trimmed. The script itself is written to a temp file and evaluated
        by ``bash -c``, so its size is not capped by the kernel's argument limit while ``$0``
        and error prefixes stay those of ``bash -c``.

        Returns a tuple of (stdout, stderr, exit_code).
        Raises BashCommandExecutionTimeoutError if the timeout is exceeded.
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
        # Everything from here to the cleanup is one try/finally, so a cancellation at any await
        # still reaches the paths. The write is shielded and awaited in the cleanup because the
        # worker thread keeps using the descriptors after a cancelled await has returned.
        write: Optional[asyncio.Future[None]] = None
        try:
            if through_sudo:
                # sudo cannot pass the environment through intact, so the trampoline reads it.
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
