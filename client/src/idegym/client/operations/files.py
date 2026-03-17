from typing import Optional
from uuid import UUID

from idegym.api.paths import ToolsPath
from idegym.api.tools.file import CreateFileRequest, EditFileRequest, FileResult, PatchFileRequest
from idegym.client.operations.forwarding import ForwardingOperations
from idegym.client.operations.utils import HTTPUtils, PollingConfig


class FileOperations:
    def __init__(self, utils: HTTPUtils, forward: ForwardingOperations) -> None:
        self._utils = utils
        self._forward = forward

    async def create_file(
        self,
        server_id: int,
        file_path: str,
        content: str,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> FileResult:
        request = CreateFileRequest(file_path=file_path, content=content)
        response_raw = await self._forward.forward_request(
            "POST", server_id, ToolsPath.CREATE_FILE, request, client_id, request_timeout, polling_config
        )
        return FileResult.model_validate(response_raw)

    async def edit_file(
        self,
        server_id: int,
        file_path: str,
        start_line: int,
        end_line: int,
        new_content: str,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> FileResult:
        request = EditFileRequest(
            file_path=file_path,
            start_line=start_line,
            end_line=end_line,
            new_content=new_content,
        )
        response_raw = await self._forward.forward_request(
            "POST", server_id, ToolsPath.EDIT_FILE, request, client_id, request_timeout, polling_config
        )
        return FileResult.model_validate(response_raw)

    async def patch_file(
        self,
        server_id: int,
        file_path: str,
        patch: str,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> FileResult:
        request = PatchFileRequest(file_path=file_path, patch=patch)
        response_raw = await self._forward.forward_request(
            "POST", server_id, ToolsPath.PATCH_FILE, request, client_id, request_timeout, polling_config
        )
        return FileResult.model_validate(response_raw)
