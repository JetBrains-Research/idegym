import asyncio
import os
import shlex
import signal
from asyncio import TimeoutError as AsyncTimeoutError
from asyncio.subprocess import Process
from importlib.resources import files
from pathlib import Path
from typing import Optional

from idegym.backend import resources
from idegym.backend.utils.environment import cleanenv
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

__BASH_INIT_FILEPATH__ = files(resources).joinpath("bash-integration.bash")


class BashExecutorError(Exception):
    pass


class BashCommandExecutionTimeoutError(BashExecutorError):
    pass


async def terminate_process_group(process: Process, graceful_termination_timeout: float = 2.0):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=graceful_termination_timeout)
        logger.info(f"Process group {process.pid} terminated gracefully")
    except AsyncTimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        logger.info(f"Process group {process.pid} was forcefully killed")
    except ProcessLookupError:
        logger.info(f"Process group {process.pid} was already terminated")


class BashExecutor:
    def __init__(self, working_directory: Optional[Path] = None):
        self.working_directory = working_directory

    async def execute_bash_command(
        self,
        command: str,
        timeout: Optional[float] = 600.0,
        graceful_termination_timeout: float = 2.0,
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
        logger.info(f"Executing bash command: {command}")

        bash_command = f"source {__BASH_INIT_FILEPATH__} && {command}"
        process = await asyncio.create_subprocess_shell(
            cmd=f"bash -c {shlex.quote(bash_command)}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.working_directory,
            preexec_fn=os.setsid,
            env=cleanenv(),
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)

            stdout_text = stdout_bytes.decode("utf-8").strip() if stdout_bytes else ""
            stderr_text = stderr_bytes.decode("utf-8").strip() if stderr_bytes else ""

            exit_code = process.returncode

        except AsyncTimeoutError:
            logger.warning(f"Command execution timed out after {timeout} seconds")
            await terminate_process_group(process, graceful_termination_timeout)

            raise BashCommandExecutionTimeoutError(f"Command execution timed out after {timeout} seconds")

        logger.info(f"Command output:\nstdout:\n{stdout_text}\n\nstderr:\n{stderr_text}")

        return stdout_text, stderr_text, exit_code
