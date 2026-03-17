from enum import StrEnum
from typing import Any, Dict

from idegym.backend.utils.bash_executor import BashExecutor
from idegym.tools.file_manager import FileManager


class ToolService:
    def __init__(self, bash_executor: BashExecutor, file_manager: FileManager):
        self.file_manager = file_manager
        self.bash_executor = bash_executor

    async def execute_tool(self, tool_name: str, parameters: Dict[str, Any]):
        match tool_name:
            case ToolName.BASH:
                command = parameters.get("command")
                timeout = parameters.get("timeout", 600.0)
                graceful_termination_timeout = parameters.get("graceful_termination_timeout", 2.0)
                if not command:
                    raise ValueError("Missing 'command' in parameters for bash tool")
                stdout, stderr, exit_code = await self.bash_executor.execute_bash_command(
                    command, timeout, graceful_termination_timeout
                )
                return stdout, stderr, exit_code
            case ToolName.FILE:
                action = parameters.get("action")
                match action:
                    case FileToolActionName.CREATE:
                        file_path = parameters.get("path")
                        content = parameters.get("content", "")
                        if not file_path:
                            raise ValueError("Missing 'path' in parameters for file creation")
                        self.file_manager.create_file(file_path, content)
                    case FileToolActionName.EDIT:
                        file_path = (
                            parameters.get("path").split("#L")[0] if "#L" in parameters.get("path", "") else None
                        )
                        line_range = (
                            parameters.get("path").split("#L")[1] if "#L" in parameters.get("path", "") else None
                        )
                        new_content = parameters.get("content", "")
                        if not file_path or not line_range:
                            raise ValueError("Missing 'path' in parameters or line range in 'path' for a file to edit")
                        start_line, end_line = self._parse_line_range(line_range)
                        self.file_manager.edit_file(file_path, start_line, end_line, new_content)
                    case FileToolActionName.PATCH:
                        file_path = parameters.get("path")
                        patch = parameters.get("patch")
                        if not file_path or not patch:
                            raise ValueError("Missing 'path' or 'patch' in parameters for file patching")
                        self.file_manager.patch_file(file_path, patch)
                    case _:
                        raise ValueError(f"Unsupported action '{action}' for file tool")
            case _:
                raise ValueError(f"Unsupported tool '{tool_name}'")

    @staticmethod
    def _parse_line_range(line_range: str):
        # Helper method to parse line range string (e.g., "100-123") to start and end line numbers
        try:
            start, end = line_range.split("-")
            return int(start), int(end)
        except ValueError:
            raise ValueError(f"Invalid line range format: '{line_range}'")


class ToolName(StrEnum):
    BASH = "bash"
    FILE = "file"


class FileToolActionName(StrEnum):
    CREATE = "create"
    EDIT = "edit"
    PATCH = "patch"
