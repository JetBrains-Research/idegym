"""
Example: Running an OpenEnv echo_env through the IdeGYM orchestrator.

echo_env is an MCP-based environment that echoes back messages via tool calls.
Available tools: echo_message(message), echo_with_length(message).
See https://huggingface.co/spaces/openenv/echo_env for the full spec.

Environment variables
---------------------
ECHO_ENV_IMAGE_TAG      - OCI image tag for the echo_env container (required)
IDEGYM_ORCHESTRATOR_URL - orchestrator URL (default: http://idegym.test)
IDEGYM_AUTH_USERNAME    - orchestrator basic-auth username (not required for idegym.test)
IDEGYM_AUTH_PASSWORD    - orchestrator basic-auth password (not required for idegym.test)

See README.md for setup and image build instructions.
"""

import os
from asyncio import run

from dotenv import load_dotenv
from echo_env import EchoEnv
from idegym.api.auth import BasicAuth
from idegym.api.orchestrator.servers import ServerKind
from idegym.client.client import IdeGYMClient
from idegym.utils.logging import get_logger

load_dotenv()
logger = get_logger(__name__)

# Docker image built from https://huggingface.co/spaces/openenv/echo_env
# See README.md for how to build and load it into Minikube.
ECHO_ENV_IMAGE_TAG = os.environ["ECHO_ENV_IMAGE_TAG"]

# IdeGYM orchestrator URL. For local deployment via Minikube (documentation/local_deployment.md),
# the default is http://idegym.test which requires no authentication.
ORCHESTRATOR_URL = os.getenv("IDEGYM_ORCHESTRATOR_URL", "http://idegym.test")


async def main():
    async with IdeGYMClient(
        orchestrator_url=ORCHESTRATOR_URL,
        name="echo-env-client",
        namespace="idegym",
        auth=BasicAuth(
            username=os.getenv("IDEGYM_AUTH_USERNAME"),
            password=os.getenv("IDEGYM_AUTH_PASSWORD"),
        ),
    ) as client:
        async with client.with_server(
            image_tag=ECHO_ENV_IMAGE_TAG,
            server_name="echo-env-server",
            server_kind=ServerKind.OPENENV,
            # echo_env runs uvicorn on port 8000 (see CMD in the Dockerfile).
            service_port=8000,
            container_port=8000,
            namespace="idegym",
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "512Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
            server_start_wait_timeout_in_seconds=120,
        ) as server:
            logger.info(f"Server started (id={server.server_id})")
            logger.info(f"openenv_url: {server.openenv_url}")

            # EchoEnv is async — use `async with` and `await`.
            async with EchoEnv(base_url=server.openenv_url) as echo:
                # Reset starts a new episode.
                await echo.reset()

                # Discover available MCP tools (echo_message, echo_with_length).
                tools = await echo.list_tools()
                logger.info(event="tools", names=[t.name for t in tools])

                # Call echo_message — returns the echoed string directly.
                messages = ["Hello, OpenEnv!", "IdeGYM running with OpenEnv server", "Done."]
                for message in messages:
                    echoed = await echo.call_tool("echo_message", message=message)
                    logger.info(event="echo_message", sent=message, echoed=echoed)

                # Call echo_with_length — returns {"message": ..., "length": ...}.
                result = await echo.call_tool("echo_with_length", message="Hello, IdeGYM!")
                logger.info(event="echo_with_length", result=result)

            logger.info("Example completed successfully!")


if __name__ == "__main__":
    run(main())
