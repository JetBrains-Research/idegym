"""Chunked binary file transfer: the server-side chunk primitives and the client-side loops.

The point of the feature is that arbitrary bytes survive a path that is JSON text end to end,
so every assertion here works on payloads that are not valid UTF-8.
"""

from base64 import b64decode, b64encode
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from idegym.api.paths import ToolsPath
from idegym.api.tools.file import DownloadFileChunkRequest, UploadFileChunkRequest
from idegym.client.operations.files import FileOperations
from idegym.tools.file_manager import FileManager
from idegym.tools.tool_service import FileToolActionName, ToolName, ToolService
from pydantic import ValidationError

BINARY = bytes(range(256)) * 4


class _FakeSandbox:
    """A stand-in server that keeps uploaded files in memory and speaks the chunk protocol."""

    def __init__(self, files: dict[str, bytes] | None = None, max_chunk: int = 1_000_000):
        self.files = dict(files or {})
        self.max_chunk = max_chunk
        self.calls: list[tuple[str, dict]] = []

    async def forward_request(self, method, server_id, path, body, client_id, request_timeout, polling_config):
        self.calls.append((path, body.model_dump()))
        if path == ToolsPath.UPLOAD_FILE:
            return self._upload(body)
        if path == ToolsPath.DOWNLOAD_FILE:
            return self._download(body)
        raise AssertionError(f"unexpected path {path}")

    def _upload(self, body: UploadFileChunkRequest) -> dict:
        data = b64decode(body.content_base64, validate=True)
        current = self.files.get(body.file_path, b"").ljust(body.offset, b"\0")
        updated = current[: body.offset] + data
        if not body.truncate:
            updated += current[body.offset + len(data) :]
        self.files[body.file_path] = updated
        return {"file_path": body.file_path, "bytes_written": len(data), "size": len(updated)}

    def _download(self, body: DownloadFileChunkRequest) -> dict:
        content = self.files[body.file_path]
        chunk = content[body.offset : body.offset + min(body.length, self.max_chunk)]
        return {
            "file_path": body.file_path,
            "offset": body.offset,
            "content_base64": b64encode(chunk).decode("ascii"),
            "bytes_read": len(chunk),
            "size": len(content),
            "eof": body.offset + len(chunk) >= len(content),
        }


def _operations(sandbox: _FakeSandbox) -> FileOperations:
    forward = AsyncMock()
    forward.forward_request = AsyncMock(side_effect=sandbox.forward_request)
    return FileOperations(utils=AsyncMock(), forward=forward)


# --------------------------------------------------------------------------------------
# FileManager chunk primitives
# --------------------------------------------------------------------------------------


def test_write_chunk_creates_missing_parents_and_reports_size(tmp_path) -> None:
    manager = FileManager()
    target = tmp_path / "nested" / "deep" / "blob.bin"

    written, size = manager.write_chunk(target, BINARY)

    assert (written, size) == (len(BINARY), len(BINARY))
    assert target.read_bytes() == BINARY


def test_write_chunk_appends_at_offset_without_losing_earlier_chunks(tmp_path) -> None:
    manager = FileManager()
    target = tmp_path / "blob.bin"

    manager.write_chunk(target, BINARY[:100])
    _written, size = manager.write_chunk(target, BINARY[100:], offset=100)

    assert size == len(BINARY)
    assert target.read_bytes() == BINARY


def test_write_chunk_truncates_the_tail_of_a_longer_previous_file(tmp_path) -> None:
    manager = FileManager()
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\xff" * 500)

    manager.write_chunk(target, b"\x00\x01")

    assert target.read_bytes() == b"\x00\x01"


def test_write_chunk_can_keep_the_tail_for_an_out_of_order_write(tmp_path) -> None:
    manager = FileManager()
    target = tmp_path / "blob.bin"
    target.write_bytes(b"abcdef")

    manager.write_chunk(target, b"XY", offset=2, truncate=False)

    assert target.read_bytes() == b"abXYef"


def test_read_chunk_returns_a_window_and_the_total_size(tmp_path) -> None:
    manager = FileManager()
    target = tmp_path / "blob.bin"
    target.write_bytes(BINARY)

    data, size = manager.read_chunk(target, offset=10, length=32)

    assert data == BINARY[10:42]
    assert size == len(BINARY)


def test_read_chunk_rejects_a_missing_file_and_a_directory(tmp_path) -> None:
    manager = FileManager()

    with pytest.raises(FileNotFoundError):
        manager.read_chunk(tmp_path / "absent.bin")
    with pytest.raises(IsADirectoryError):
        manager.read_chunk(tmp_path)


def test_chunk_primitives_respect_the_working_directory(tmp_path) -> None:
    manager = FileManager(working_directory=tmp_path)

    manager.write_chunk(Path("relative.bin"), BINARY)

    assert (tmp_path / "relative.bin").read_bytes() == BINARY
    assert manager.read_chunk(Path("relative.bin"))[0] == BINARY


# --------------------------------------------------------------------------------------
# ToolService actions
# --------------------------------------------------------------------------------------


async def test_tool_service_round_trips_binary_through_base64(tmp_path) -> None:
    service = ToolService(bash_executor=AsyncMock(), file_manager=FileManager())
    target = str(tmp_path / "blob.bin")

    uploaded = await service.execute_tool(
        ToolName.FILE,
        {
            "action": FileToolActionName.UPLOAD,
            "path": target,
            "content_base64": b64encode(BINARY).decode("ascii"),
        },
    )
    downloaded = await service.execute_tool(
        ToolName.FILE,
        {"action": FileToolActionName.DOWNLOAD, "path": target, "length": len(BINARY)},
    )

    assert uploaded.size == len(BINARY)
    assert b64decode(downloaded.content_base64) == BINARY
    assert downloaded.eof is True


async def test_tool_service_download_reports_not_at_eof_for_a_partial_window(tmp_path) -> None:
    service = ToolService(bash_executor=AsyncMock(), file_manager=FileManager())
    target = tmp_path / "blob.bin"
    target.write_bytes(BINARY)

    response = await service.execute_tool(
        ToolName.FILE,
        {"action": FileToolActionName.DOWNLOAD, "path": str(target), "length": 16},
    )

    assert response.bytes_read == 16
    assert response.size == len(BINARY)
    assert response.eof is False


# --------------------------------------------------------------------------------------
# Client chunking loops
# --------------------------------------------------------------------------------------


async def test_upload_bytes_splits_into_chunks_and_reassembles_intact() -> None:
    sandbox = _FakeSandbox()
    operations = _operations(sandbox)

    size = await operations.upload_bytes(server_id=1, file_path="/work/blob.bin", data=BINARY, chunk_bytes=100)

    assert size == len(BINARY)
    assert sandbox.files["/work/blob.bin"] == BINARY
    assert len(sandbox.calls) == 11


async def test_upload_of_empty_content_still_creates_the_file() -> None:
    sandbox = _FakeSandbox()
    operations = _operations(sandbox)

    size = await operations.upload_bytes(server_id=1, file_path="/work/empty.bin", data=b"")

    assert size == 0
    assert sandbox.files["/work/empty.bin"] == b""
    assert len(sandbox.calls) == 1


async def test_download_bytes_follows_eof_across_chunks() -> None:
    sandbox = _FakeSandbox({"/work/blob.bin": BINARY}, max_chunk=64)
    operations = _operations(sandbox)

    assert await operations.download_bytes(server_id=1, file_path="/work/blob.bin") == BINARY
    assert len(sandbox.calls) == 16


async def test_download_of_an_empty_file_returns_no_bytes() -> None:
    sandbox = _FakeSandbox({"/work/empty.bin": b""})
    operations = _operations(sandbox)

    assert await operations.download_bytes(server_id=1, file_path="/work/empty.bin") == b""


async def test_upload_and_download_file_round_trip_on_disk(tmp_path) -> None:
    sandbox = _FakeSandbox(max_chunk=97)
    operations = _operations(sandbox)
    source = tmp_path / "source.bin"
    source.write_bytes(BINARY)
    destination = tmp_path / "restored" / "copy.bin"

    await operations.upload_file(server_id=1, local_path=source, file_path="/work/blob.bin", chunk_bytes=97)
    written = await operations.download_file(
        server_id=1, file_path="/work/blob.bin", local_path=destination, chunk_bytes=97
    )

    assert written == len(BINARY)
    assert destination.read_bytes() == BINARY


@pytest.mark.parametrize("chunk_bytes", [0, -1])
async def test_transfer_rejects_a_non_positive_chunk_size(chunk_bytes) -> None:
    operations = _operations(_FakeSandbox({"/work/blob.bin": BINARY}))

    with pytest.raises(ValueError, match="chunk_bytes"):
        await operations.upload_bytes(server_id=1, file_path="/work/blob.bin", data=BINARY, chunk_bytes=chunk_bytes)
    with pytest.raises(ValueError, match="chunk_bytes"):
        await operations.download_bytes(server_id=1, file_path="/work/blob.bin", chunk_bytes=chunk_bytes)


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (UploadFileChunkRequest, {"content_base64": "", "offset": -1}),
        (DownloadFileChunkRequest, {"offset": -1}),
        (DownloadFileChunkRequest, {"length": 0}),
    ],
)
def test_chunk_requests_reject_out_of_range_windows(model, kwargs) -> None:
    with pytest.raises(ValidationError):
        model(file_path="/work/blob.bin", **kwargs)
