"""
Example: Running a TBench2 environment through the IdeGYM orchestrator.

See https://huggingface.co/spaces/openenv/tbench2/tree/main for the full API.

Environment variables
---------------------
TBENCH2_IMAGE_TAG       - OCI image tag for the tbench2 container (required)
IDEGYM_ORCHESTRATOR_URL - orchestrator URL (default: http://idegym.test)
IDEGYM_AUTH_USERNAME    - orchestrator basic-auth username (not required for idegym.test)
IDEGYM_AUTH_PASSWORD    - orchestrator basic-auth password (not required for idegym.test)

See README.md for setup and image build instructions.
"""

import os
from asyncio import run

from dotenv import load_dotenv
from idegym.api.auth import BasicAuth
from idegym.api.orchestrator.servers import ServerKind
from idegym.client.client import IdeGYMClient
from idegym.utils.logging import get_logger
from tbench2_env import Tbench2Action, Tbench2Env

load_dotenv()
logger = get_logger(__name__)

# Docker image built from https://huggingface.co/spaces/openenv/tbench2
# See README.md for how to build and load it into Minikube.
TBENCH2_IMAGE_TAG = os.environ["TBENCH2_IMAGE_TAG"]

# A task ID from the Terminal-Bench-2 task suite.
# Replace with the task you want to evaluate.
TASK_ID = "headless-terminal"

# IdeGYM orchestrator URL. For local deployment via Minikube (documentation/local_deployment.md),
# the default is http://idegym.test which requires no authentication.
ORCHESTRATOR_URL = os.getenv("IDEGYM_ORCHESTRATOR_URL", "http://idegym.test")


async def main():
    async with IdeGYMClient(
        orchestrator_url=ORCHESTRATOR_URL,
        name="tbench2-client",
        namespace="idegym",
        auth=BasicAuth(
            username=os.getenv("IDEGYM_AUTH_USERNAME"),
            password=os.getenv("IDEGYM_AUTH_PASSWORD"),
        ),
    ) as client:
        async with client.with_server(
            image_tag=TBENCH2_IMAGE_TAG,
            server_name="tbench2-server",
            server_kind=ServerKind.OPENENV,
            # tbench2 runs uvicorn on port 8000.
            service_port=8000,
            container_port=8000,
            namespace="idegym",
            runtime_class_name="gvisor",
            resources={
                "requests": {"cpu": "500m", "memory": "512Mi"},
                "limits": {"cpu": "2", "memory": "2Gi"},
            },
            server_start_wait_timeout_in_seconds=120,
        ) as server:
            logger.info(f"Server started (id={server.server_id})")
            logger.info(f"openenv_url: {server.openenv_url}")

            # Tbench2Env is async — use `async with` and `await`.
            async with Tbench2Env(base_url=server.openenv_url) as tbench:
                # Reset to the chosen task — returns the task instruction.
                reset_result = await tbench.reset(task_id=TASK_ID)
                observation = reset_result.observation
                logger.info(
                    event="reset",
                    task_id=observation.task_id,
                    task_path=observation.task_path,
                    instruction=observation.instruction,
                )

                # --- agent loop ---

                # Explore the task directory.
                result = await tbench.step(Tbench2Action(action_type="exec", command="ls -la"))
                logger.info(event="ls", output=result.observation.output)

                # Read the task instruction from the environment.
                result = await tbench.step(Tbench2Action(action_type="exec", command="cat instruction.md"))
                logger.info(event="readme", output=result.observation.output)

                # Write a solution file.
                result = await tbench.step(
                    Tbench2Action(
                        action_type="write_file",
                        file_path="solution.py",
                        content="# solution placeholder\nprint('hello')\n",
                    )
                )
                logger.info(
                    event="write_file",
                    success=result.observation.success,
                    error=result.observation.error,
                )

                # Run the solution and check output.
                result = await tbench.step(Tbench2Action(action_type="exec", command="python solution.py"))
                logger.info(event="run_solution", output=result.observation.output)

                # Evaluate the task — runs the test suite and returns a binary reward.
                eval_result = await tbench.step(Tbench2Action(action_type="evaluate"))
                logger.info(
                    event="evaluate",
                    reward=eval_result.reward,
                    done=eval_result.done,
                    output=eval_result.observation.output,
                )

            logger.info("Example completed successfully!")


if __name__ == "__main__":
    run(main())
