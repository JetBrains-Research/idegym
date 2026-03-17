from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock

from idegym.api.status import Status
from idegym.rewards.setup_checker import SetupChecker


class TestSetupChecker(IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_bash_executor = AsyncMock()
        self.setup_checker = SetupChecker(self.mock_bash_executor)

    async def test_check_repository_setup_success(self):
        checker_script = "check_script.sh"
        self.mock_bash_executor.execute_bash_command.return_value = ("", "", 0)

        result = await self.setup_checker.check_repository_setup(checker_script, 600.0, 3.0)

        self.mock_bash_executor.execute_bash_command.assert_awaited_once_with(checker_script, 600.0, 3.0)
        self.assertEqual(result, {"status": Status.SUCCESS, "output": ""})

    async def test_check_repository_setup_failure(self):
        checker_script = "check_script.sh"
        stdout = "Some output"
        error_message = "Error: Something went wrong"

        self.mock_bash_executor.execute_bash_command.return_value = (stdout, error_message, 1)

        result = await self.setup_checker.check_repository_setup(checker_script, 600.0, 3.0)

        self.mock_bash_executor.execute_bash_command.assert_awaited_once_with(checker_script, 600.0, 3.0)

        expected_command_output = f"stdout:\n{stdout}\n\nstderr:\n{error_message}"
        self.assertEqual(result, {"status": Status.FAILURE, "output": expected_command_output})

    async def test_check_repository_setup_empty_script(self):
        checker_script = ""
        self.mock_bash_executor.execute_bash_command.return_value = ("", "", 0)

        result = await self.setup_checker.check_repository_setup(checker_script, 600.0, 3.0)

        self.mock_bash_executor.execute_bash_command.assert_awaited_once_with(checker_script, 600.0, 3.0)
        self.assertEqual(result, {"status": Status.SUCCESS, "output": ""})


if __name__ == "__main__":
    main()
