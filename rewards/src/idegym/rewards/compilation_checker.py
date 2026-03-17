from idegym.api.status import Status
from idegym.backend.utils.bash_executor import BashExecutor


class CompilationChecker:
    def __init__(self, bash_executor: BashExecutor):
        self.bash_executor = bash_executor

    async def check_repository_compilation(
        self,
        compilation_script: str = "./gradlew assemble",
        timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
    ) -> dict:
        stdout, stderr, exit_code = await self.bash_executor.execute_bash_command(
            compilation_script, timeout, graceful_termination_timeout
        )

        if exit_code == 0:
            return {"status": Status.SUCCESS, "output": ""}
        else:
            error_lines = "\n".join(line for line in stderr.splitlines() if line.startswith("e:"))
            return {"status": Status.FAILURE, "output": error_lines}
