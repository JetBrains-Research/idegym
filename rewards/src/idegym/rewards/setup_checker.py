from idegym.api.status import Status
from idegym.backend.utils.bash_executor import BashExecutor


class SetupChecker:
    def __init__(self, bash_executor: BashExecutor):
        self.bash_executor = bash_executor

    async def check_repository_setup(
        self, checker_script: str, timeout: float, graceful_termination_timeout: float = 2.0
    ) -> dict:
        stdout, stderr, exit_code = await self.bash_executor.execute_bash_command(
            checker_script, timeout, graceful_termination_timeout
        )

        if exit_code == 0:
            return {"status": Status.SUCCESS, "output": ""}
        else:
            command_output = f"stdout:\n{stdout}\n\nstderr:\n{stderr}"
            return {"status": Status.FAILURE, "output": command_output}
