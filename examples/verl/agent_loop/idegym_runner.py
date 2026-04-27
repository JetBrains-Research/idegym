"""
IDEGym cloud test execution pipeline.

Manages IdeGYMClient/Server lifecycle and provides methods for running
bash commands, editing files, and executing tests on remote IDEGym servers.
"""

import asyncio
import logging
import os
import time
from typing import Any, Optional

import aiohttp
from dotenv import load_dotenv
from idegym.api.exceptions import IdeGYMException
from idegym.api.orchestrator.servers import ServerReuseStrategy
from idegym.client.client import IdeGYMClient
from idegym.client.server import IdeGYMServer
from kubernetes_asyncio.client import V1ResourceRequirements

from examples.verl.agent_loop.idegym_runner_utils import ItemToRun
from examples.verl.utils.postprocessing import extract_bash_output

logger = logging.getLogger(__name__)
load_dotenv()


def with_timeout_and_retry(max_retries: int = 3, retry_delay: float = 1.0, timeout: float = 10.0):
    """Decorator for async methods that adds timeout and retry logic."""

    def decorator(func):
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    result = await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
                    return result
                except (
                    IdeGYMException,
                    aiohttp.client_exceptions.ServerDisconnectedError,
                    aiohttp.client_exceptions.ClientConnectorError,
                    asyncio.TimeoutError,
                    ConnectionError,
                ) as e:
                    logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {type(e).__name__}: {e}")
                    if attempt == max_retries - 1:
                        logger.warning("Could not run the command with IDEGYM. Exiting.")
                        raise
                    await asyncio.sleep(retry_delay)
                except Exception as e:
                    logger.error(f"Non-retryable error: {type(e).__name__}: {e}")
                    raise e

        return wrapper

    return decorator


# IDEGym configuration via environment variables
NAMESPACE = os.getenv("NAMESPACE", "idegym")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL")
CLIENT_NAME = os.getenv("CLIENT_NAME", "django-agent")
REPO = os.getenv("REPO", "django")
IMAGE_TAG = os.getenv("IMAGE_TAG")
if ORCHESTRATOR_URL is None:
    raise ValueError("ORCHESTRATOR_URL environment variable must be set")
if IMAGE_TAG is None:
    raise ValueError("IMAGE_TAG environment variable must be set")

HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", 120))
RESOURCES = V1ResourceRequirements(
    requests={"cpu": "750m", "memory": "1024Mi", "ephemeral-storage": "5Gi"},
    limits={"cpu": "3000m", "memory": "5120Mi", "ephemeral-storage": "5Gi"},
)
SERVER_START_TIMEOUT = int(os.getenv("SERVER_START_TIMEOUT", 600))
CLIENT_START_TIMEOUT = float(os.getenv("CLIENT_START_TIMEOUT", 120.0))

MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", 1.0))

TEST_COMMAND_TIMEOUT = int(os.getenv("TEST_COMMAND_TIMEOUT", 60))
RESET_TIMEOUT = float(os.getenv("RESET_TIMEOUT", 10.0))
SHORT_BASH_CMD_TIMEOUT = float(os.getenv("SHORT_BASH_CMD_TIMEOUT", 60.0))
EDIT_TIMEOUT = float(os.getenv("EDIT_TIMEOUT", 60.0))
MAX_RETRIES_SETUP = int(os.getenv("MAX_RETRIES_SETUP", 3))
NETWORK_BUFFER = float(os.getenv("NETWORK_BUFFER", 10.0))


class IDEGymRunner:
    def __init__(
        self,
        max_retries: int = MAX_RETRIES,
        retry_delay: float = RETRY_DELAY,
        repository: str = REPO,
        **kwargs,
    ):
        """Initialize the IDEGym test pipeline for cloud execution."""
        self.image_tag = IMAGE_TAG
        self.server_resources = RESOURCES
        self.server_start_timeout = SERVER_START_TIMEOUT
        self.client_start_timeout = CLIENT_START_TIMEOUT
        self.namespace = NAMESPACE
        self.orchestrator_url = ORCHESTRATOR_URL
        self.client_name = CLIENT_NAME
        self.server_name = CLIENT_NAME

        self.test_command_timeout = kwargs.get("test_command_timeout", TEST_COMMAND_TIMEOUT)
        self.reset_timeout = kwargs.get("reset_timeout", RESET_TIMEOUT)
        self.short_bash_cmd_timeout = kwargs.get("short_bash_cmd_timeout", SHORT_BASH_CMD_TIMEOUT)
        self.edit_timeout = kwargs.get("edit_timeout", EDIT_TIMEOUT)
        self.heartbeat_interval = kwargs.get("heartbeat_interval", HEARTBEAT_INTERVAL)
        self.max_retries_setup = kwargs.get("max_retries_setup", MAX_RETRIES_SETUP)

        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.repository = repository

        auth_username = os.getenv("IDEGYM_AUTH_USERNAME") or os.getenv("BASIC_AUTH_USERNAME")
        auth_password = os.getenv("IDEGYM_AUTH_PASSWORD") or os.getenv("BASIC_AUTH_PASSWORD")

        if not auth_username or not auth_password:
            raise RuntimeError(
                "Environment variables IDEGYM_AUTH_USERNAME/BASIC_AUTH_USERNAME and "
                "IDEGYM_AUTH_PASSWORD/BASIC_AUTH_PASSWORD must be provided."
            )

        logger.info("Initializing IDEGymRunner")

    async def create_client(self) -> IdeGYMClient:
        """Create and initialize an IDEGYM client."""
        logger.debug(f"Initializing IDEGYM client '{self.client_name}'")
        client = IdeGYMClient(
            name=self.client_name,
            orchestrator_url=self.orchestrator_url,
            namespace=self.namespace,
            heartbeat_interval_in_seconds=self.heartbeat_interval,
        )
        await asyncio.wait_for(client.health_check(), timeout=self.client_start_timeout)
        await asyncio.wait_for(client.__aenter__(), timeout=self.client_start_timeout)
        logger.debug(f"IDEGYM client '{self.client_name}' initialized successfully")
        return client

    async def close_client(self, client: Optional[IdeGYMClient]) -> None:
        """Stop the IDEGYM client and clean up resources."""
        if client is None:
            return
        logger.info(f"Finalizing IDEGYM client '{client.name}'")
        try:
            await asyncio.wait_for(client.__aexit__(None, None, None), timeout=self.client_start_timeout)
            logger.info(f"IDEGYM client '{client.name}' finalized successfully")
        except Exception as e:
            logger.error(f"Error finalizing IDEGYM client: {e}")

    async def create_server(self, client: IdeGYMClient) -> IdeGYMServer:
        """Create a new IDEGYM server instance."""
        logger.debug(f"Creating server '{self.server_name}' with image {self.image_tag}")
        server = await client.start_server(
            image_tag=self.image_tag,
            server_name=self.server_name,
            namespace=self.namespace,
            resources=self.server_resources,
            server_start_wait_timeout_in_seconds=self.server_start_timeout,
            reuse_strategy=ServerReuseStrategy.RESET,
        )
        logger.debug(f"Successfully created server {self.server_name}: {server.server_id}")
        return server

    async def finish_server(self, server: IdeGYMServer) -> None:
        """Stop and clean up a server instance."""
        try:
            logger.info(f"Stopping server {server.server_id}")
            await asyncio.wait_for(server._finish_server(), timeout=self.client_start_timeout)
            logger.info(f"Server {server.server_id} finished successfully")
        except Exception as e:
            logger.error(f"Error finishing server {server.server_id}: {e}")


    async def run_bash(self, server: IdeGYMServer, command: str) -> dict[str, Any]:
        """Execute a bash command on the given server."""
        command_res = await server.execute_bash(script=command, command_timeout=self.test_command_timeout)
        return {
            "server_id": server.server_id,
            "command": command,
            "command_output": {
                "stdout": command_res.stdout,
                "stderr": command_res.stderr,
                "exit_code": command_res.exit_code,
            },
        }

    @with_timeout_and_retry(max_retries=3, timeout=2 * SHORT_BASH_CMD_TIMEOUT + TEST_COMMAND_TIMEOUT + NETWORK_BUFFER)
    async def run_tests(self, server: IdeGYMServer, item: ItemToRun, do_edit: bool = True) -> dict[str, Any]:
        """Execute tests for the given item on the server."""
        tests_str = " ".join(item.tests)
        if self.repository == "sympy":
            test_command = f"python bin/test {tests_str}"
        elif self.repository == "django":
            test_command = f"python tests/runtests.py {tests_str} --verbosity 0 --noinput"
        else:
            raise ValueError(f"Invalid repository: {self.repository}")

        time_start_global = time.perf_counter()
        if do_edit:
            await server.edit_file(
                file_path=item.file_path,
                start_line=item.start_line,
                end_line=item.end_line,
                new_content=item.replace_content,
                request_timeout=self.short_bash_cmd_timeout,
            )
        time_edit = time.perf_counter() - time_start_global

        time_start = time.perf_counter()
        cat_res = await server.execute_bash(
            script=f"cat {item.file_path}",
            command_timeout=self.short_bash_cmd_timeout,
        )
        time_cat = time.perf_counter() - time_start

        time_start = time.perf_counter()
        test_result = await server.execute_bash(script=test_command, command_timeout=self.test_command_timeout)
        time_test = time.perf_counter() - time_start

        test_output = extract_bash_output(test_result)
        edited_file = extract_bash_output(cat_res)

        time_total = time.perf_counter() - time_start_global

        return {
            "datapoint": item,
            "test_output": test_output,
            "edited_file": edited_file,
            "time_test": time_test,
            "time_cat": time_cat,
            "time_edit": time_edit,
            "time_total": time_total,
            "server_id": server.server_id,
        }

    async def edit_file(self, server: IdeGYMServer, item: ItemToRun) -> dict[str, Any] | None:
        """Edit a file on the given server."""
        await server.edit_file(
            file_path=item.file_path,
            start_line=item.start_line,
            end_line=item.end_line,
            new_content=item.replace_content,
            request_timeout=self.edit_timeout,
        )
        cat_res = await server.execute_bash(script=f"cat {item.file_path}", command_timeout=self.short_bash_cmd_timeout)
        edited_file = extract_bash_output(cat_res)
        return {
            "server_id": server.server_id,
            "edited_file": edited_file,
        }

    async def reset_server(self, server: IdeGYMServer) -> None:
        """Reset the project state on the given server."""
        await server.reset_project(
            reset_timeout=self.reset_timeout,
            graceful_termination_timeout=5.0,
        )
        logger.info(f"Reset server {server.server_id}")
