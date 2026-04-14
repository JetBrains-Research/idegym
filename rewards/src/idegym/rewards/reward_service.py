from enum import StrEnum

from idegym.backend.utils.bash_executor import BashExecutor
from idegym.rewards.compilation_checker import CompilationChecker
from idegym.rewards.setup_checker import SetupChecker
from idegym.rewards.test_checker import TestChecker


class RewardName(StrEnum):
    COMPILATION = "compilation"
    TEST = "test"
    SETUP = "setup"


class RewardService:
    def __init__(self, bash_executor: BashExecutor):
        self.compilation_checker = CompilationChecker(bash_executor)
        self.setup_checker = SetupChecker(bash_executor)
        self.test_checker = TestChecker(bash_executor)

    async def collect_reward(self, reward_name: RewardName, **kwargs) -> dict:
        match reward_name:
            case RewardName.COMPILATION:
                return await self._compilation_checker(
                    kwargs.get("compilation_script", "./gradlew assemble"),
                    kwargs.get("timeout"),
                    kwargs.get("graceful_termination_timeout"),
                )
            case RewardName.TEST:
                return await self._test_checker(
                    kwargs.get("test_script", "python -m pytest --junitxml=test-results.xml"),
                    kwargs.get("timeout"),
                    kwargs.get("graceful_termination_timeout"),
                )
            case RewardName.SETUP:
                return await self._setup_checker(
                    kwargs.get("setup_check_script", "echo OK"),
                    kwargs.get("timeout"),
                    kwargs.get("graceful_termination_timeout"),
                )
            case _:
                raise ValueError(f"Unknown reward name: {reward_name}")

    async def _compilation_checker(
        self, compilation_script: str, timeout: float, graceful_termination_timeout: float
    ) -> dict:
        result = await self.compilation_checker.check_repository_compilation(
            compilation_script, timeout, graceful_termination_timeout
        )
        return {"reward": RewardName.COMPILATION.value, **result}

    async def _test_checker(self, test_script: str, timeout: float, graceful_termination_timeout: float) -> dict:
        result = await self.test_checker.check_repository_tests(test_script, timeout, graceful_termination_timeout)
        return {"reward": RewardName.TEST.value, **result}

    async def _setup_checker(
        self, setup_check_script: str, timeout: float, graceful_termination_timeout: float
    ) -> dict:
        result = await self.setup_checker.check_repository_setup(
            setup_check_script, timeout, graceful_termination_timeout
        )
        return {"reward": RewardName.SETUP.value, **result}
