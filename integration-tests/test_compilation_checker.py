from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock

from idegym.api.status import Status
from idegym.rewards.compilation_checker import CompilationChecker


class TestCompilationChecker(IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_bash_executor = AsyncMock()
        self.compilation_checker = CompilationChecker(self.mock_bash_executor)

    async def test_check_repository_compilation_success(self):
        compilation_script = "./gradlew assemble"
        timeout = 600.0
        graceful_termination_timeout = 3.0
        self.mock_bash_executor.execute_bash_command.return_value = ("", "", 0)

        result = await self.compilation_checker.check_repository_compilation(
            compilation_script, timeout, graceful_termination_timeout
        )

        self.mock_bash_executor.execute_bash_command.assert_awaited_once_with(
            compilation_script, timeout, graceful_termination_timeout
        )
        self.assertEqual(result, {"status": Status.SUCCESS, "output": ""})

    async def test_check_repository_compilation_failure_with_error_lines(self):
        compilation_script = "./gradlew assemble"
        timeout = 600.0
        graceful_termination_timeout = 3.0
        mock_stderr = "e: Some error occurred\nSome non-error line\ne: Another error message"
        self.mock_bash_executor.execute_bash_command.return_value = ("", mock_stderr, 1)

        result = await self.compilation_checker.check_repository_compilation(
            compilation_script, timeout, graceful_termination_timeout
        )

        self.mock_bash_executor.execute_bash_command.assert_awaited_once_with(
            compilation_script, timeout, graceful_termination_timeout
        )
        self.assertEqual(
            result, {"status": Status.FAILURE, "output": "e: Some error occurred\ne: Another error message"}
        )

    async def test_check_repository_compilation_failure_no_error_lines(self):
        compilation_script = "./gradlew assemble"
        timeout = 600.0
        graceful_termination_timeout = 3.0
        mock_stdout = "Some non-error line\nAnother non-error line"
        self.mock_bash_executor.execute_bash_command.return_value = (mock_stdout, "", 1)

        result = await self.compilation_checker.check_repository_compilation(
            compilation_script, timeout, graceful_termination_timeout
        )

        self.mock_bash_executor.execute_bash_command.assert_awaited_once_with(
            compilation_script, timeout, graceful_termination_timeout
        )
        self.assertEqual(result, {"status": Status.FAILURE, "output": ""})

    async def test_check_repository_compilation_custom_script(self):
        custom_script = "./custom_build.sh"
        timeout = 600.0
        graceful_termination_timeout = 3.0
        self.mock_bash_executor.execute_bash_command.return_value = ("", "", 0)

        result = await self.compilation_checker.check_repository_compilation(
            custom_script, timeout, graceful_termination_timeout
        )

        self.mock_bash_executor.execute_bash_command.assert_awaited_once_with(
            custom_script, timeout, graceful_termination_timeout
        )
        self.assertEqual(result, {"status": Status.SUCCESS, "output": ""})

    async def test_check_repository_compilation_empty_path(self):
        compilation_script = "./gradlew assemble"
        with self.assertRaises(ValueError):
            await self.compilation_checker.check_repository_compilation(compilation_script)

    async def test_check_repository_compilation_empty_script(self):
        compilation_script = ""
        with self.assertRaises(ValueError):
            await self.compilation_checker.check_repository_compilation(compilation_script)


if __name__ == "__main__":
    main()
