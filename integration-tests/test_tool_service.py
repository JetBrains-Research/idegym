from asyncio import Future
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import MagicMock

from idegym.tools.tool_service import ToolService

# TODO: Rewrite with pytest!


class TestToolService(IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = ToolService(
            bash_executor=MagicMock(),
            file_manager=MagicMock(),
        )

    async def test_execute_bash_tool_success(self):
        stdout = "Some output"
        stderr = "Some error"

        # Mocking async return value
        self.service.bash_executor.execute_bash_command.return_value = Future()
        self.service.bash_executor.execute_bash_command.return_value.set_result((stdout, stderr, 0))
        parameters = {"command": "echo 'Hello'", "timeout": 600.0, "graceful_termination_timeout": 3.0}

        # Await the async method call
        result = await self.service.execute_tool("bash", parameters)

        self.assertEqual(result, (stdout, stderr, 0))
        self.service.bash_executor.execute_bash_command.assert_called_once_with("echo 'Hello'", 600.0, 3.0)

    async def test_execute_bash_tool_missing_command(self):
        parameters = {}

        with self.assertRaises(ValueError) as context:
            await self.service.execute_tool("bash", parameters)
        self.assertEqual(str(context.exception), "Missing 'command' in parameters for bash tool")

    async def test_execute_file_tool_create_success(self):
        parameters = {"action": "create", "path": "/tmp/test.txt", "content": "Hello, world!"}

        await self.service.execute_tool("file", parameters)

        self.service.file_manager.create_file.assert_called_once_with("/tmp/test.txt", "Hello, world!")

    async def test_execute_file_tool_create_missing_path(self):
        parameters = {"action": "create", "content": "Hello, world!"}

        with self.assertRaises(ValueError) as context:
            await self.service.execute_tool("file", parameters)
        self.assertEqual(str(context.exception), "Missing 'path' in parameters for file creation")

    async def test_execute_file_tool_edit_success(self):
        parameters = {"action": "edit", "path": "/tmp/test.txt#L1-5", "content": "New content"}

        await self.service.execute_tool("file", parameters)

        self.service.file_manager.edit_file.assert_called_once_with("/tmp/test.txt", 1, 5, "New content")

    async def test_execute_file_tool_edit_missing_path_or_range(self):
        parameters = {"action": "edit", "content": "New content"}

        with self.assertRaises(ValueError) as context:
            await self.service.execute_tool("file", parameters)
        self.assertEqual(
            str(context.exception), "Missing 'path' in parameters or line range in 'path' for a file to edit"
        )

    async def test_execute_file_tool_edit_invalid_range_format(self):
        parameters = {"action": "edit", "path": "/tmp/test.txt#1-5", "content": "New content"}

        with self.assertRaises(ValueError) as context:
            await self.service.execute_tool("file", parameters)
        self.assertEqual(
            str(context.exception), "Missing 'path' in parameters or line range in 'path' for a file to edit"
        )

    async def test_execute_unsupported_tool(self):
        parameters = {}
        with self.assertRaises(ValueError) as context:
            await self.service.execute_tool("unsupported_tool", parameters)
        self.assertEqual(str(context.exception), "Unsupported tool 'unsupported_tool'")

    async def test_parse_line_range_success(self):
        range_result = ToolService._parse_line_range("10-20")
        self.assertEqual(range_result, (10, 20))

    async def test_parse_line_range_invalid_format(self):
        with self.assertRaises(ValueError) as context:
            ToolService._parse_line_range("10:20")
        self.assertEqual(str(context.exception), "Invalid line range format: '10:20'")

    async def test_parse_line_range_non_numeric(self):
        with self.assertRaises(ValueError) as context:
            ToolService._parse_line_range("10-XX")
        self.assertEqual(str(context.exception), "Invalid line range format: '10-XX'")


if __name__ == "__main__":
    main()
