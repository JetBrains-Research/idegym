"""Binary file transfer through the orchestrator, end to end.

The forwarding path stores and replays a request as JSON *text*, so this suite works entirely
with payloads that are not valid UTF-8: if any hop decoded them, the comparison would fail.
"""

import pytest
from utils.constants import DEFAULT_SERVER_START_TIMEOUT
from utils.idegym_utils import create_http_client

BINARY = bytes(range(256)) * 64


@pytest.mark.asyncio
async def test_binary_round_trip_through_the_orchestrator(test_image, test_id, tmp_path):
    async with (
        create_http_client(name=f"transfer-{test_id}", nodes_count=0) as client,
        client.with_server(
            image_tag=test_image,
            server_name=f"transfer-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server,
    ):
        size = await server.upload_bytes("/tmp/blob.bin", BINARY, chunk_bytes=4096)
        assert size == len(BINARY)

        assert await server.download_bytes("/tmp/blob.bin", chunk_bytes=4096) == BINARY

        destination = tmp_path / "restored.bin"
        written = await server.download_file("/tmp/blob.bin", destination, chunk_bytes=4096)
        assert written == len(BINARY)
        assert destination.read_bytes() == BINARY


@pytest.mark.asyncio
async def test_uploaded_file_is_visible_to_the_bash_tool(test_image, test_id, tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(BINARY)

    async with (
        create_http_client(name=f"transfer-bash-{test_id}", nodes_count=0) as client,
        client.with_server(
            image_tag=test_image,
            server_name=f"transfer-bash-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server,
    ):
        await server.upload_file(source, "/tmp/nested/source.bin", chunk_bytes=4096)

        result = await server.execute_bash(script="wc -c < /tmp/nested/source.bin")
        assert result.exit_code == 0
        assert result.stdout.strip() == str(len(BINARY))


@pytest.mark.asyncio
async def test_downloading_a_missing_file_fails(test_image, test_id):
    async with (
        create_http_client(name=f"transfer-missing-{test_id}", nodes_count=0) as client,
        client.with_server(
            image_tag=test_image,
            server_name=f"transfer-missing-{test_id}",
            runtime_class_name="gvisor",
            run_as_root=True,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server,
    ):
        with pytest.raises(Exception, match="404"):
            await server.download_bytes("/tmp/definitely-absent.bin")
