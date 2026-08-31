"""FastAPI router for tools endpoints.

Uses FastAPI's native ``dependency_overrides`` mechanism instead of
``dependency_injector``. The server registers the real ``ToolService``
implementation via ``app.dependency_overrides[_get_tool_service] = ...``
before starting to serve requests.
"""

from binascii import Error as Base64Error

from fastapi import APIRouter, Depends, HTTPException, status
from idegym.api.paths import ToolsPath
from idegym.api.status import Status
from idegym.api.tools.bash import BashCommandRequest, BashCommandResponse
from idegym.api.tools.file import (
    CreateFileRequest,
    DownloadFileChunkRequest,
    DownloadFileChunkResponse,
    EditFileRequest,
    FileResult,
    PatchFileRequest,
    UploadFileChunkRequest,
    UploadFileChunkResponse,
)
from idegym.tools.tool_service import FileToolActionName, ToolName, ToolService

router = APIRouter()


async def _get_tool_service() -> ToolService:
    """Stub dependency — server overrides this via ``app.dependency_overrides``."""
    raise RuntimeError("tool_service not configured")


@router.post(ToolsPath.BASH)
async def execute_bash_script(
    request: BashCommandRequest,
    service: ToolService = Depends(_get_tool_service),
):
    stdout, stderr, exit_code = await service.execute_tool(
        tool_name=ToolName.BASH,
        parameters={
            "command": request.command,
            "timeout": request.timeout,
            "graceful_termination_timeout": request.graceful_termination_timeout,
            "max_output_bytes": request.max_output_bytes,
            "strip_output": request.strip_output,
        },
    )

    return BashCommandResponse(stdout=stdout, stderr=stderr, exit_code=exit_code)


@router.post(ToolsPath.CREATE_FILE)
async def create_file(
    request: CreateFileRequest,
    service: ToolService = Depends(_get_tool_service),
):
    await service.execute_tool(
        tool_name=ToolName.FILE,
        parameters={
            "action": FileToolActionName.CREATE,
            "path": request.file_path,
            "content": request.content,
        },
    )

    return FileResult(status=Status.SUCCESS)


@router.post(ToolsPath.EDIT_FILE)
async def replace_lines(
    request: EditFileRequest,
    service: ToolService = Depends(_get_tool_service),
):
    await service.execute_tool(
        tool_name=ToolName.FILE,
        parameters={
            "action": FileToolActionName.EDIT,
            "path": request.file_path + "#L" + str(request.start_line) + "-" + str(request.end_line),
            "content": request.new_content,
        },
    )

    return FileResult(status=Status.SUCCESS)


@router.post(ToolsPath.PATCH_FILE)
async def patch_file(
    request: PatchFileRequest,
    service: ToolService = Depends(_get_tool_service),
):
    await service.execute_tool(
        tool_name=ToolName.FILE,
        parameters={
            "action": FileToolActionName.PATCH,
            "path": request.file_path,
            "patch": request.patch,
        },
    )

    return FileResult(status=Status.SUCCESS)


@router.post(ToolsPath.UPLOAD_FILE)
async def upload_file_chunk(
    request: UploadFileChunkRequest,
    service: ToolService = Depends(_get_tool_service),
) -> UploadFileChunkResponse:
    """Write one base64 chunk into a file, so binary survives the JSON forwarding path."""
    try:
        return await service.execute_tool(
            tool_name=ToolName.FILE,
            parameters={
                "action": FileToolActionName.UPLOAD,
                "path": request.file_path,
                "content_base64": request.content_base64,
                "offset": request.offset,
                "truncate": request.truncate,
            },
        )
    except Base64Error as ex:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid base64 content: {ex}") from ex
    except (NotADirectoryError, IsADirectoryError) as ex:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ex)) from ex


@router.post(ToolsPath.DOWNLOAD_FILE)
async def download_file_chunk(
    request: DownloadFileChunkRequest,
    service: ToolService = Depends(_get_tool_service),
) -> DownloadFileChunkResponse:
    """Read one base64 chunk of a file, so binary survives the JSON forwarding path."""
    try:
        return await service.execute_tool(
            tool_name=ToolName.FILE,
            parameters={
                "action": FileToolActionName.DOWNLOAD,
                "path": request.file_path,
                "offset": request.offset,
                "length": request.length,
            },
        )
    except FileNotFoundError as ex:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ex)) from ex
    except IsADirectoryError as ex:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ex)) from ex
