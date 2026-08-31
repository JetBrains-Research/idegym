import asyncio
from pathlib import Path

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


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("export TOKEN=s3cr3t", "export TOKEN=<redacted>"),
        ("export TOKEN='s3 cr3t'", "export TOKEN=<redacted>"),
        ('export TOKEN="s3 cr3t"', "export TOKEN=<redacted>"),
        ("export EMPTY=", "export EMPTY=<redacted>"),
        ("export A=1; export B=2 && echo done", "export A=<redacted>; export B=<redacted> && echo done"),
        ("exporting TOKEN=keepme", "exporting TOKEN=keepme"),
        ("TOKEN=keepme echo hi", "TOKEN=keepme echo hi"),
    ],
)
def test_redact_exports_masks_only_the_assigned_value(command, expected) -> None:
    assert bash_executor._redact_exports(command) == expected


def test_command_excerpt_redacts_before_truncating() -> None:
    command = "export TOKEN=" + "s" * 4000

    excerpt = bash_executor._command_excerpt(command)

    assert "s" * 20 not in excerpt
    assert excerpt.startswith("export TOKEN=<redacted>")


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


# --------------------------------------------------------------------------------------
# Per-command context: cwd, env, user
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "requested", "expected"),
    [
        (None, None, None),
        (Path("/root/work"), None, Path("/root/work")),
        (Path("/root/work"), "src", Path("/root/work/src")),
        (Path("/root/work"), "/etc", Path("/etc")),
        (None, "src", Path("src")),
    ],
)
def test_resolve_working_directory_treats_relative_paths_as_project_relative(base, requested, expected) -> None:
    executor = bash_executor.BashExecutor(working_directory=base)

    assert executor.resolve_working_directory(requested) == expected


def test_argv_runs_the_script_file_directly_when_no_user_is_requested() -> None:
    assert bash_executor._process_argv("/tmp/script.sh", None) == ["bash", "/tmp/script.sh"]


def test_argv_drops_to_a_user_without_re_authenticating() -> None:
    argv = bash_executor._process_argv("/tmp/script.sh", "devuser")

    assert argv == ["runuser", "--preserve-environment", "-u", "devuser", "--", "bash", "/tmp/script.sh"]


def test_argv_passes_a_hostile_user_name_as_one_argument() -> None:
    """No shell parses this argv, so a name with metacharacters is inert rather than quoted."""
    argv = bash_executor._process_argv("/tmp/script.sh", "dev; rm -rf /")

    assert "dev; rm -rf /" in argv


def test_script_file_is_private_unless_another_user_must_read_it() -> None:
    private = bash_executor._write_script("echo hi", readable_by_other_user=False)
    shared = bash_executor._write_script("echo hi", readable_by_other_user=True)

    try:
        assert Path(private).read_text() == "echo hi"
        assert Path(private).stat().st_mode & 0o777 == 0o600
        assert Path(shared).stat().st_mode & 0o777 == 0o644
    finally:
        bash_executor._remove_script(private)
        bash_executor._remove_script(shared)


def test_removing_the_script_twice_is_not_an_error() -> None:
    path = bash_executor._write_script("echo hi", readable_by_other_user=False)

    bash_executor._remove_script(path)
    bash_executor._remove_script(path)

    assert not Path(path).exists()


def test_bash_request_defaults_to_no_per_command_context() -> None:
    request = BashCommandRequest(command="echo hello")

    assert (request.cwd, request.env, request.user) == (None, {}, None)


def test_init_prefix_uses_a_separator_so_it_cannot_gate_the_script() -> None:
    prefixed = bash_executor._prepend_bash_integration("a; b; c")

    assert "&&" not in prefixed
    assert prefixed.endswith(" ; a; b; c")
    assert "\n" not in prefixed


def test_init_prefix_quotes_the_init_path() -> None:
    prefixed = bash_executor._prepend_bash_integration("true")

    assert str(bash_executor.__BASH_INIT_FILEPATH__) in prefixed.replace("'", "")
