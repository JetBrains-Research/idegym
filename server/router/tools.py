from typing import Annotated

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter
from fastapi.params import Depends
from idegym.api.paths import ToolsPath
from idegym.api.status import Status
from idegym.api.tools.bash import BashCommandRequest, BashCommandResponse
from idegym.api.tools.file import CreateFileRequest, EditFileRequest, FileResult, PatchFileRequest
from idegym.tools.tool_service import FileToolActionName, ToolName, ToolService

from server.dependencies import Container

router = APIRouter()


@router.post(ToolsPath.BASH)
@inject
async def execute_bash_script(
    request: BashCommandRequest,
    service: Annotated[ToolService, Depends(Provide[Container.tool_service])],
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
@inject
async def create_file(
    request: CreateFileRequest,
    service: Annotated[ToolService, Depends(Provide[Container.tool_service])],
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
@inject
async def replace_lines(
    request: EditFileRequest,
    service: Annotated[ToolService, Depends(Provide[Container.tool_service])],
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
@inject
async def patch_file(
    request: PatchFileRequest,
    service: Annotated[ToolService, Depends(Provide[Container.tool_service])],
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
