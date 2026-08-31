import asyncio

import pytest
from idegym.api.tools.bash import BashCommandRequest
from idegym.backend.utils import bash_executor
from pydantic import ValidationError


def _stream(*chunks: bytes) -> asyncio.StreamReader:
    stream = asyncio.StreamReader()
    for chunk in chunks:
        stream.feed_data(chunk)
    stream.feed_eof()
    return stream


@pytest.mark.parametrize("payload", [b"short", b"12345678"])
async def test_read_bounded_preserves_output_within_limit(payload) -> None:
    retained, total = await bash_executor._read_bounded(_stream(payload), 8)

    assert retained == payload
    assert total == len(payload)


async def test_read_bounded_preserves_odd_head_and_tail_across_chunks() -> None:
    retained, total = await bash_executor._read_bounded(
        _stream(b"abc", b"defgh", b"ijklmno"),
        7,
    )

    assert retained.startswith(b"abc\n... [IdeGYM truncated 8 output bytes] ...\n")
    assert retained.endswith(b"lmno")
    assert total == 15


async def test_read_bounded_supports_one_byte_limit() -> None:
    retained, total = await bash_executor._read_bounded(_stream(b"abc"), 1)

    assert retained == b"\n... [IdeGYM truncated 2 output bytes] ...\nc"
    assert total == 3


async def test_read_bounded_none_retains_complete_multichunk_output() -> None:
    retained, total = await bash_executor._read_bounded(_stream(b"head", b"middle", b"tail"), None)

    assert retained == b"headmiddletail"
    assert total == 14


@pytest.mark.parametrize(
    ("limit", "error"),
    [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError)],
)
async def test_read_bounded_rejects_invalid_limit(limit, error) -> None:
    with pytest.raises(error, match="max_output_bytes"):
        await bash_executor._read_bounded(_stream(b"output"), limit)


@pytest.mark.parametrize("exit_error", [ProcessLookupError, PermissionError])
async def test_terminate_process_group_tolerates_exit_before_sigkill(monkeypatch, mocker, exit_error) -> None:
    process = mocker.Mock(pid=123, returncode=-15)
    process.send_signal.side_effect = ProcessLookupError
    signals = []

    def kill_process_group(pid, requested_signal) -> None:
        signals.append((pid, requested_signal))
        if requested_signal == bash_executor.signal.SIGKILL:
            raise exit_error

    async def group_did_not_exit(process_group_id, timeout) -> bool:
        return False

    monkeypatch.setattr(bash_executor.os, "killpg", kill_process_group)
    monkeypatch.setattr(bash_executor, "_wait_for_process_group_exit", group_did_not_exit)

    await bash_executor.terminate_process_group(process, graceful_termination_timeout=0.001)

    assert signals == [
        (process.pid, bash_executor.signal.SIGTERM),
        (process.pid, bash_executor.signal.SIGKILL),
    ]


def test_signal_process_group_tolerates_permission_race_before_returncode_update(monkeypatch, mocker) -> None:
    process = mocker.Mock(pid=123, returncode=None)
    process.send_signal.side_effect = ProcessLookupError
    monkeypatch.setattr(bash_executor.os, "killpg", mocker.Mock(side_effect=PermissionError))

    assert not bash_executor._signal_process_group(process, bash_executor.signal.SIGKILL)
    process.send_signal.assert_called_once_with(bash_executor.signal.SIGKILL)


async def test_finish_output_drain_closes_pipes_and_cancels_reader(monkeypatch, mocker) -> None:
    stdout_transport = mocker.Mock()
    stderr_transport = mocker.Mock()
    process = mocker.Mock()
    process._transport.get_pipe_transport.side_effect = [stdout_transport, stderr_transport]
    communication_task = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(bash_executor, "_OUTPUT_DRAIN_TIMEOUT_SECONDS", 0)

    await bash_executor._finish_output_drain(process, communication_task)

    stdout_transport.close.assert_called_once_with()
    stderr_transport.close.assert_called_once_with()
    assert communication_task.cancelled()


def test_collector_exposes_bounded_partial_output() -> None:
    collector = bash_executor._OutputCollector(8)
    collector.append(b"abcdefgh")
    collector.append(b"ijkl")

    assert collector.retained().startswith(b"abcd\n... [IdeGYM truncated 4 output bytes] ...\n")
    assert collector.retained().endswith(b"ijkl")
    assert collector.total == 12


def test_decode_output_replaces_invalid_utf8_and_preserves_surrounding_whitespace() -> None:
    assert bash_executor._decode_output(b"  indented\xff\n") == "  indented�\n"


def test_decode_output_trims_only_when_asked() -> None:
    assert bash_executor._decode_output(b"\n  spaced  \n", strip=True) == "spaced"


def test_decode_output_of_empty_stream_is_empty_either_way() -> None:
    assert bash_executor._decode_output(b"") == ""
    assert bash_executor._decode_output(b"", strip=True) == ""


def test_log_excerpt_is_bounded_to_one_page() -> None:
    excerpt = bash_executor._log_excerpt("x" * 4000)

    assert excerpt.startswith("x" * 900)
    assert excerpt.endswith("x" * 900)
    assert "log excerpt truncated" in excerpt


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_bash_request_rejects_invalid_output_limit(limit) -> None:
    with pytest.raises(ValidationError):
        BashCommandRequest(command="echo hello", max_output_bytes=limit)


def test_bash_request_supports_explicit_unlimited_output() -> None:
    assert BashCommandRequest(command="echo hello", max_output_bytes=None).max_output_bytes is None


def test_bash_request_keeps_output_verbatim_by_default() -> None:
    assert BashCommandRequest(command="echo hello").strip_output is False
