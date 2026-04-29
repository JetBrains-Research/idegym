"""FastAPI router for tools endpoints.

Uses FastAPI's native ``dependency_overrides`` mechanism instead of
``dependency_injector``. The server registers the real ``ToolService``
implementation via ``app.dependency_overrides[_get_tool_service] = ...``
before starting to serve requests.
"""

from fastapi import APIRouter, Depends
from idegym.api.paths import ToolsPath
from idegym.api.status import Status
from idegym.api.tools.bash import BashCommandRequest, BashCommandResponse
from idegym.api.tools.file import CreateFileRequest, EditFileRequest, FileResult, PatchFileRequest
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
