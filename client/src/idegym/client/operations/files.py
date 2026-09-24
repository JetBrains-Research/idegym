import asyncio
from base64 import b64decode, b64encode
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import BinaryIO, Optional
from uuid import UUID

from idegym.api.paths import ToolsPath
from idegym.api.tools.file import (
    DEFAULT_FILE_CHUNK_BYTES,
    CreateFileRequest,
    DownloadFileChunkRequest,
    DownloadFileChunkResponse,
    EditFileRequest,
    FileResult,
    PatchFileRequest,
    UploadFileChunkRequest,
    UploadFileChunkResponse,
)
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

    async def upload_chunk(
        self,
        server_id: int,
        file_path: str,
        data: bytes,
        offset: int = 0,
        truncate: bool = True,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> UploadFileChunkResponse:
        """Write one chunk of raw bytes into a file on the server."""
        request = UploadFileChunkRequest(
            file_path=file_path,
            content_base64=b64encode(data).decode("ascii"),
            offset=offset,
            truncate=truncate,
        )
        response_raw = await self._forward.forward_request(
            "POST", server_id, ToolsPath.UPLOAD_FILE, request, client_id, request_timeout, polling_config
        )
        return UploadFileChunkResponse.model_validate(response_raw)

    async def download_chunk(
        self,
        server_id: int,
        file_path: str,
        offset: int = 0,
        length: int = DEFAULT_FILE_CHUNK_BYTES,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> DownloadFileChunkResponse:
        """Read one chunk of raw bytes from a file on the server."""
        request = DownloadFileChunkRequest(file_path=file_path, offset=offset, length=length)
        response_raw = await self._forward.forward_request(
            "POST", server_id, ToolsPath.DOWNLOAD_FILE, request, client_id, request_timeout, polling_config
        )
        return DownloadFileChunkResponse.model_validate(response_raw)

    async def upload_bytes(
        self,
        server_id: int,
        file_path: str,
        data: bytes,
        chunk_bytes: int = DEFAULT_FILE_CHUNK_BYTES,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> int:
        """Upload ``data`` to ``file_path`` on the server and return the resulting file size."""

        async def read_slice(offset: int) -> bytes:
            return data[offset : offset + chunk_bytes]

        return await self._upload_stream(
            server_id=server_id,
            file_path=file_path,
            read_chunk_at=read_slice,
            total=len(data),
            chunk_bytes=chunk_bytes,
            client_id=client_id,
            request_timeout=request_timeout,
            polling_config=polling_config,
        )

    async def upload_file(
        self,
        server_id: int,
        local_path: Path | str,
        file_path: str,
        chunk_bytes: int = DEFAULT_FILE_CHUNK_BYTES,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> int:
        """Upload a local file to ``file_path`` on the server and return the resulting file size.

        The file is read one chunk at a time, so its size is bounded by the sandbox's disk rather
        than by the client's memory.
        """
        source = Path(local_path)
        total = await asyncio.to_thread(lambda: source.stat().st_size)
        handle = await asyncio.to_thread(source.open, "rb")

        async def read_file_at(offset: int) -> bytes:
            return await asyncio.to_thread(_read_at, handle, offset, chunk_bytes)

        try:
            return await self._upload_stream(
                server_id=server_id,
                file_path=file_path,
                read_chunk_at=read_file_at,
                total=total,
                chunk_bytes=chunk_bytes,
                client_id=client_id,
                request_timeout=request_timeout,
                polling_config=polling_config,
            )
        finally:
            await asyncio.to_thread(handle.close)

    async def download_bytes(
        self,
        server_id: int,
        file_path: str,
        chunk_bytes: int = DEFAULT_FILE_CHUNK_BYTES,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> bytes:
        """Download ``file_path`` from the server and return its bytes."""
        collected = bytearray()
        async for chunk in self._download_chunks(
            server_id=server_id,
            file_path=file_path,
            chunk_bytes=chunk_bytes,
            client_id=client_id,
            request_timeout=request_timeout,
            polling_config=polling_config,
        ):
            collected.extend(chunk)
        return bytes(collected)

    async def download_file(
        self,
        server_id: int,
        file_path: str,
        local_path: Path | str,
        chunk_bytes: int = DEFAULT_FILE_CHUNK_BYTES,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> int:
        """Download ``file_path`` from the server into ``local_path`` and return the byte count."""
        destination = Path(local_path)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        handle = await asyncio.to_thread(destination.open, "wb")
        written = 0
        try:
            async for chunk in self._download_chunks(
                server_id=server_id,
                file_path=file_path,
                chunk_bytes=chunk_bytes,
                client_id=client_id,
                request_timeout=request_timeout,
                polling_config=polling_config,
            ):
                written += await asyncio.to_thread(handle.write, chunk)
        finally:
            await asyncio.to_thread(handle.close)
        return written

    async def _upload_stream(
        self,
        server_id: int,
        file_path: str,
        read_chunk_at: Callable[[int], Awaitable[bytes]],
        total: int,
        chunk_bytes: int,
        client_id: Optional[UUID],
        request_timeout: Optional[int],
        polling_config: PollingConfig,
    ) -> int:
        if chunk_bytes < 1:
            raise ValueError("chunk_bytes must be positive")

        offset = 0
        # An empty source still sends one request, otherwise the file would never be created.
        while True:
            chunk = await read_chunk_at(offset)
            response = await self.upload_chunk(
                server_id=server_id,
                file_path=file_path,
                data=chunk,
                offset=offset,
                client_id=client_id,
                request_timeout=request_timeout,
                polling_config=polling_config,
            )
            offset += response.bytes_written
            if offset >= total:
                return response.size
            if not chunk:
                # The loop advances by bytes written, so a short read before `total` would spin
                # forever sending empty chunks. Happens when the source shrinks mid-upload:
                # `total` was measured up front, and reads past the new end return nothing.
                raise RuntimeError(
                    f"Upload source ended at {offset} bytes, before the expected {total}: "
                    f"it shrank while {file_path} was being uploaded"
                )

    async def _download_chunks(
        self,
        server_id: int,
        file_path: str,
        chunk_bytes: int,
        client_id: Optional[UUID],
        request_timeout: Optional[int],
        polling_config: PollingConfig,
    ) -> AsyncIterator[bytes]:
        if chunk_bytes < 1:
            raise ValueError("chunk_bytes must be positive")

        offset = 0
        while True:
            response = await self.download_chunk(
                server_id=server_id,
                file_path=file_path,
                offset=offset,
                length=chunk_bytes,
                client_id=client_id,
                request_timeout=request_timeout,
                polling_config=polling_config,
            )
            chunk = b64decode(response.content_base64, validate=True)
            if chunk:
                yield chunk
            offset += len(chunk)
            # `eof` ends a normal transfer; an empty chunk guards against a file shrinking mid-read.
            if response.eof or not chunk:
                return


def _read_at(handle: BinaryIO, offset: int, length: int) -> bytes:
    handle.seek(offset)
    return handle.read(length)
