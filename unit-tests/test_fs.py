from pathlib import Path

from fastapi import HTTPException
from pytest import fixture, mark, param, raises

from server.router.fs import valid_workspace_path


@fixture
def root() -> Path:
    return Path(".project").resolve()


@mark.parametrize(
    "path",
    [
        param("", id="empty"),
        param("dir", id="directory"),
        param("file.txt", id="file"),
        param("dir/file.txt", id="path"),
    ],
)
@mark.asyncio
async def test_valid_path(path: str, root: Path):
    result = await valid_workspace_path(path, str(root))
    assert result == root / path


@mark.parametrize(
    "path",
    [
        param("../outside", id="parent-directory-traversal"),
        param("../../etc/passwd", id="multi-level-parent-traversal"),
        param("test_dir/../../../etc/passwd", id="complex-multi-level-traversal"),
        param("test_dir/./../../etc/passwd", id="dot-parent-combination-traversal"),
    ],
)
@mark.asyncio
async def test_directory_traversal(path: str, root: Path):
    with raises(HTTPException) as ex:
        await valid_workspace_path(path, str(root))
    assert ex.value.status_code == 403


@mark.asyncio
async def test_malformed_path(root: Path):
    with raises(HTTPException) as ex:
        await valid_workspace_path("test\x00dir", str(root))
    assert ex.value.status_code == 400
