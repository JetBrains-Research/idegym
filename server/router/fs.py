from typing import Annotated, Optional

from anyio import Path
from anyio import open_file as open
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from idegym.api.data import DataSize
from idegym.api.paths import FSPath

from server.dependencies import Container

router = APIRouter()


@inject
async def valid_workspace_path(
    path: Optional[str],
    root: Annotated[str, Depends(Provide[Container.config.project.path])],
) -> Path:
    try:
        workspace = await Path(root).resolve()
        if not path:
            return workspace
        target = await (workspace / path).resolve()
    except Exception as ex:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(ex),
        ) from ex
    if not str(target).startswith(str(workspace)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return target


async def rmtree(path: Path):
    if await path.is_dir():
        async for entry in path.iterdir():
            await rmtree(entry)
        await path.rmdir()
    else:
        await path.unlink()


@router.get(f"{FSPath.LIST_DIRECTORY}/{{path:path}}")
async def ls(path: Path = Depends(valid_workspace_path)):
    if not await path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Path not found: {path}")
    if not await path.is_dir():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Path is not a directory: {path}")
    content = [entry.name async for entry in path.iterdir()]
    return JSONResponse(content=content)


@router.get(f"{FSPath.READ_FILE}/{{path:path}}")
@inject
async def cat(
    size: Annotated[DataSize, Depends(Provide[Container.config.server.response_buffer_size])],
    path: Path = Depends(valid_workspace_path),
):
    if not await path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Path not found: {path}")
    if not await path.is_file():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Path is not a file: {path}")

    async def reader():
        async with await open(path, "rb") as file:
            while chunk := await file.read(size.bytes):
                yield chunk

    return StreamingResponse(content=reader(), media_type="application/octet-stream")


@router.put(f"{FSPath.CREATE_FILE}/{{path:path}}")
async def touch(path: Path = Depends(valid_workspace_path)):
    if not await path.parent.exists() or not await path.parent.is_dir():
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    response = Response(status_code=status.HTTP_201_CREATED if not await path.exists() else status.HTTP_200_OK)
    await path.touch()
    if not await path.exists():
        return Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return response


@router.put(f"{FSPath.CREATE_DIRECTORY}/{{path:path}}")
async def mkdir(path: Path = Depends(valid_workspace_path), parents: bool = False):
    if await path.exists():
        return Response(status_code=status.HTTP_200_OK if await path.is_dir() else status.HTTP_400_BAD_REQUEST)
    if not parents and not await path.parent.exists():
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    await path.mkdir(parents=parents)
    return Response(status_code=status.HTTP_201_CREATED)


@router.delete(f"{FSPath.DELETE_PATH}/{{path:path}}")
@inject
async def rm(
    root: Annotated[str, Depends(Provide[Container.config.project.path])],
    path: Path = Depends(valid_workspace_path),
    recursive: bool = False,
):
    if not await path.exists():
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    if path == await Path(root).resolve():
        return Response(status_code=status.HTTP_403_FORBIDDEN)
    if await path.is_file():
        await path.unlink()
    elif recursive:
        await rmtree(path)
    else:
        if [_ async for _ in path.iterdir()]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Directory is not empty. Use recursive=true to delete it.",
            )
        await path.rmdir()
    return Response(status_code=status.HTTP_200_OK)
