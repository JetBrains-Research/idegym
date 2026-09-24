from base64 import b64decode, b64encode
from enum import StrEnum
from typing import Any

from idegym.api.tools.bash import DEFAULT_MAX_OUTPUT_BYTES
from idegym.api.tools.file import (
    DEFAULT_FILE_CHUNK_BYTES,
    DownloadFileChunkResponse,
    UploadFileChunkResponse,
)
from idegym.backend.utils.bash_executor import BashExecutor
from idegym.tools.file_manager import FileManager


class ToolName(StrEnum):
    BASH = "bash"
    FILE = "file"


class FileToolActionName(StrEnum):
    CREATE = "create"
    EDIT = "edit"
    PATCH = "patch"
    UPLOAD = "upload"
    DOWNLOAD = "download"


class ToolService:
    def __init__(self, bash_executor: BashExecutor, file_manager: FileManager):
        self.file_manager = file_manager
        self.bash_executor = bash_executor

    async def execute_tool(self, tool_name: str, parameters: dict[str, Any]):
        match tool_name:
            case ToolName.BASH:
                command = parameters.get("command")
                timeout = parameters.get("timeout", 600.0)
                graceful_termination_timeout = parameters.get("graceful_termination_timeout", 2.0)
                max_output_bytes = parameters.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
                strip_output = parameters.get("strip_output", False)
                if not command:
                    raise ValueError("Missing 'command' in parameters for bash tool")
                stdout, stderr, exit_code = await self.bash_executor.execute_bash_command(
                    command=command,
                    timeout=timeout,
                    graceful_termination_timeout=graceful_termination_timeout,
                    max_output_bytes=max_output_bytes,
                    strip_output=strip_output,
                    cwd=parameters.get("cwd"),
                    env=parameters.get("env"),
                    user=parameters.get("user"),
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
                        # Line range is encoded in the path as "file.py#L100-123" (GitHub-style anchor)
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
                    case FileToolActionName.UPLOAD:
                        file_path = parameters.get("path")
                        if not file_path:
                            raise ValueError("Missing 'path' in parameters for file upload")
                        bytes_written, size = self.file_manager.write_chunk(
                            file_path,
                            b64decode(parameters.get("content_base64", ""), validate=True),
                            offset=parameters.get("offset", 0),
                            truncate=parameters.get("truncate", True),
                        )
                        return UploadFileChunkResponse(file_path=file_path, bytes_written=bytes_written, size=size)
                    case FileToolActionName.DOWNLOAD:
                        file_path = parameters.get("path")
                        if not file_path:
                            raise ValueError("Missing 'path' in parameters for file download")
                        offset = parameters.get("offset", 0)
                        data, size = self.file_manager.read_chunk(
                            file_path,
                            offset=offset,
                            length=parameters.get("length", DEFAULT_FILE_CHUNK_BYTES),
                        )
                        return DownloadFileChunkResponse(
                            file_path=file_path,
                            offset=offset,
                            content_base64=b64encode(data).decode("ascii"),
                            bytes_read=len(data),
                            size=size,
                            eof=offset + len(data) >= size,
                        )
                    case _:
                        raise ValueError(f"Unsupported action '{action}' for file tool")
            case _:
                raise ValueError(f"Unsupported tool '{tool_name}'")

    @staticmethod
    def _parse_line_range(line_range: str) -> tuple[int, int]:
        try:
            start, end = line_range.split("-")
            return int(start), int(end)
        except ValueError:
            raise ValueError(f"Invalid line range format: '{line_range}'")
