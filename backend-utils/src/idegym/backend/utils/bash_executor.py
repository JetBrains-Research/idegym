import asyncio
import os
import shlex
import signal
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


class BashExecutorError(Exception):
    pass


class BashCommandExecutionTimeoutError(BashExecutorError):
    pass


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


def _decode_output(output: bytes) -> str:
    return output.decode("utf-8", errors="replace").rstrip() if output else ""


def _log_excerpt(text: str) -> str:
    if len(text) <= _LOG_EXCERPT_CHARS:
        return text
    half = _LOG_EXCERPT_CHARS // 2
    return f"{text[:half]}\n... [log excerpt truncated] ...\n{text[-half:]}"


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

    async def execute_bash_command(
        self,
        command: str,
        timeout: Optional[float] = 600.0,
        graceful_termination_timeout: float = 2.0,
        max_output_bytes: Optional[int] = None,
    ) -> tuple[str, str, int]:
        """
        Execute a bash command asynchronously.

        The command runs inside a bash-integration environment (sourced from a
        bundled init script) in a clean subprocess environment with IdeGYM-specific
        variables stripped. The process is started in its own process group so the
        entire group can be killed on timeout.

        Returns a tuple of (stdout, stderr, exit_code).
        Raises BashCommandExecutionTimeoutError if the timeout is exceeded.
        """
        stdout_collector = _OutputCollector(max_output_bytes)
        stderr_collector = _OutputCollector(max_output_bytes)
        logger.info("Executing bash command", command=_log_excerpt(command))

        bash_command = f"source {__BASH_INIT_FILEPATH__} && {command}"
        process = await asyncio.create_subprocess_shell(
            cmd=f"bash -c {shlex.quote(bash_command)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_directory,
            preexec_fn=os.setsid,
            env=cleanenv(),
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

        if timed_out:
            logger.warning(
                "Command execution timed out",
                timeout_seconds=timeout,
                stdout_bytes=stdout_collector.total,
                stderr_bytes=stderr_collector.total,
                stdout=_log_excerpt(_decode_output(stdout_collector.retained())),
                stderr=_log_excerpt(_decode_output(stderr_collector.retained())),
            )
            raise BashCommandExecutionTimeoutError(f"Command execution timed out after {timeout} seconds")

        stdout_text = _decode_output(stdout_collector.retained())
        stderr_text = _decode_output(stderr_collector.retained())
        exit_code = process.returncode
        if exit_code is None:
            raise RuntimeError("Bash process completed without an exit code")

        logger.info(
            "Command completed",
            exit_code=exit_code,
            stdout_bytes=stdout_collector.total,
            stderr_bytes=stderr_collector.total,
            stdout=_log_excerpt(stdout_text),
            stderr=_log_excerpt(stderr_text),
        )

        return stdout_text, stderr_text, exit_code
