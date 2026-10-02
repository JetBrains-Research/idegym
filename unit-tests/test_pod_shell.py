"""The dashboard's pod shell: the relay between the browser and the Kubernetes exec stream, and the
page and WebSocket that guard it.

The exec stream and the pod lookup are faked, so these tests pin the two protocols (the browser's
JSON frames, Kubernetes' channel-prefixed frames) and every reason a shell must not open.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from aiohttp import WSMsgType
from fastapi import FastAPI
from fastapi.testclient import TestClient
from idegym.api.config import Config
from idegym.orchestrator import pod_shell
from idegym.orchestrator.router import dashboard_shell
from starlette.requests import Request
from structlog.testing import capture_logs

HOST = "idegym.example.com"
SANDBOX = {"app.kubernetes.io/part-of": "idegym", "app.kubernetes.io/component": "sandbox", "app": "srv-7"}
EXITED = json.dumps({"status": "Success"}).encode()


def _frame(channel: int, payload: bytes) -> SimpleNamespace:
    return SimpleNamespace(type=WSMsgType.BINARY, data=bytes([channel]) + payload)


class FakeStream:
    """The exec WebSocket: records what the orchestrator sends and replays scripted frames."""

    def __init__(self, frames: Optional[list[Any]] = None, on_input: Optional[dict[bytes, list[Any]]] = None):
        self.sent: list[bytes] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.on_input = on_input or {}
        for frame in frames or []:
            self.queue.put_nowait(frame)

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)
        for frame in self.on_input.get(data, []):
            await self.queue.put(frame)

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.queue.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


class FakeBrowser:
    """The browser's WebSocket as ``relay`` sees it."""

    def __init__(self, messages: list[dict[str, Any]], hold_open: bool = True):
        self.inbox: asyncio.Queue = asyncio.Queue()
        for message in messages:
            self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(message)})
        if not hold_open:
            self.inbox.put_nowait({"type": "websocket.disconnect"})
        self.output = bytearray()
        self.messages: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def send_bytes(self, data: bytes) -> None:
        self.output.extend(data)

    async def send_json(self, data: dict[str, Any]) -> None:
        self.messages.append(data)


# ---- The relay -----------------------------------------------------------------------------------


async def test_keystrokes_and_resizes_reach_the_pod_on_their_channels() -> None:
    stream = FakeStream(on_input={b"\x00exit\r": [_frame(pod_shell.ERROR_CHANNEL, EXITED), None]})
    browser = FakeBrowser([{"type": "resize", "cols": 120, "rows": 40}, {"type": "input", "data": "exit\r"}])

    outcome = await pod_shell.relay(browser, stream)

    assert stream.sent[0][0] == pod_shell.RESIZE_CHANNEL
    assert json.loads(stream.sent[0][1:]) == {"Width": 120, "Height": 40}
    assert stream.sent[1] == b"\x00exit\r"
    assert outcome == pod_shell.ShellOutcome("exited", 0)


async def test_stdout_and_stderr_reach_the_browser_and_the_exit_code_is_kept() -> None:
    failure = json.dumps(
        {"status": "Failure", "details": {"causes": [{"reason": "ExitCode", "message": "3"}]}}
    ).encode()
    stream = FakeStream(
        [
            _frame(pod_shell.STDOUT_CHANNEL, b"hello "),
            _frame(pod_shell.STDERR_CHANNEL, b"world"),
            _frame(pod_shell.STDOUT_CHANNEL, b""),
            _frame(pod_shell.ERROR_CHANNEL, failure),
            None,
        ]
    )
    browser = FakeBrowser([])

    outcome = await pod_shell.relay(browser, stream)

    assert bytes(browser.output) == b"hello world"
    assert outcome == pod_shell.ShellOutcome("exited", 3)


async def test_a_command_that_never_ran_tells_the_browser_why() -> None:
    failure = json.dumps(
        {"status": "Failure", "reason": "InternalError", "message": 'executable file not found: "/bin/sh"'}
    ).encode()
    browser = FakeBrowser([])

    outcome = await pod_shell.relay(browser, FakeStream([_frame(pod_shell.ERROR_CHANNEL, failure), None]))

    assert browser.messages == [{"type": "error", "message": 'executable file not found: "/bin/sh"'}]
    assert outcome == pod_shell.ShellOutcome("exited", None)


async def test_a_clean_exit_sends_no_error() -> None:
    browser = FakeBrowser([])

    await pod_shell.relay(browser, FakeStream([_frame(pod_shell.ERROR_CHANNEL, EXITED), None]))

    assert browser.messages == []


async def test_a_closed_tab_ends_the_session() -> None:
    outcome = await pod_shell.relay(FakeBrowser([], hold_open=False), FakeStream())

    assert outcome.reason == "browser_closed"


async def test_a_dropped_exec_connection_ends_the_session() -> None:
    outcome = await pod_shell.relay(FakeBrowser([]), FakeStream([None]))

    assert outcome.reason == "pod_closed"


async def test_an_idle_session_is_closed() -> None:
    outcome = await pod_shell.relay(FakeBrowser([]), FakeStream(), idle_timeout=0.05)

    assert outcome.reason == "idle"


async def test_malformed_browser_frames_are_ignored() -> None:
    stream = FakeStream(on_input={b"\x00ls\r": [None]})
    browser = FakeBrowser([{"type": "input", "data": 42}, {"type": "mystery"}, {"type": "input", "data": "ls\r"}])
    browser.inbox.put_nowait({"type": "websocket.receive", "text": "not json"})

    await pod_shell.relay(browser, stream)

    assert stream.sent == [b"\x00ls\r"]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (b'{"status": "Success"}', 0),
        (b'{"status": "Failure", "details": {"causes": [{"reason": "ExitCode", "message": "137"}]}}', 137),
        (b'{"status": "Failure", "message": "container not found"}', None),
        (b"not json", None),
        (b"[1, 2]", None),
    ],
)
def test_exit_codes_are_read_from_the_status_object(status: bytes, code: Optional[int]) -> None:
    assert pod_shell.exit_code(status) == code


def test_only_a_failure_without_an_exit_code_has_a_message() -> None:
    assert (
        pod_shell.failure_message(b'{"status": "Failure", "message": "container not found"}') == "container not found"
    )
    assert pod_shell.failure_message(b'{"status": "Success"}') is None
    exited = b'{"status": "Failure", "details": {"causes": [{"reason": "ExitCode", "message": "1"}]}}'
    assert pod_shell.failure_message(exited) is None
    assert pod_shell.failure_message(b"garbage") is None


def test_only_idegym_server_pods_count_as_sandboxes() -> None:
    assert pod_shell.is_sandbox(SimpleNamespace(metadata=SimpleNamespace(labels=SANDBOX)))
    assert not pod_shell.is_sandbox(SimpleNamespace(metadata=SimpleNamespace(labels={"app": "postgres"})))
    assert not pod_shell.is_sandbox(SimpleNamespace(metadata=SimpleNamespace(labels=None)))


# ---- The page and the WebSocket ------------------------------------------------------------------


def _pod(labels: Optional[dict[str, str]] = None, phase: str = "Running") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name="srv-7-5d8f7c9b4-x2kqp",
            namespace="idegym",
            labels=SANDBOX if labels is None else labels,
            deletion_timestamp=None,
        ),
        spec=SimpleNamespace(node_name="node-1", containers=[SimpleNamespace(name="server")], init_containers=[]),
        status=SimpleNamespace(phase=phase, start_time=None, container_statuses=[]),
    )


def _config(enabled: bool = True) -> Config:
    config = Config()
    config.orchestrator.dashboard.shell_enabled = enabled
    return config


@pytest.fixture
def client(mocker):
    app = FastAPI()
    app.include_router(dashboard_shell.router)
    app.state.config = _config()
    read = mocker.patch.object(dashboard_shell, "_read_pod", mocker.AsyncMock(return_value=_pod()))
    return SimpleNamespace(http=TestClient(app, base_url=f"https://{HOST}"), app=app, read=read)


def _connect(client, origin: Optional[str] = f"https://{HOST}", container: str = ""):
    headers = {"host": HOST, **({"origin": origin} if origin else {})}
    query = f"?container={container}" if container else ""
    return client.http.websocket_connect(f"/dashboard/pods/idegym/srv-7-5d8f7c9b4-x2kqp/exec{query}", headers=headers)


@pytest.mark.parametrize(
    ("setup", "problem"),
    [
        (lambda c: setattr(c.app.state, "config", _config(enabled=False)), "disabled"),
        (lambda c: None, "opened from the dashboard itself"),
        (lambda c: setattr(c.read, "return_value", _pod(labels={"app": "postgres"})), "IdeGYM server pod"),
        (lambda c: setattr(c.read, "return_value", _pod(phase="Pending")), "not Running"),
    ],
    ids=["disabled", "origin", "not-a-sandbox", "not-running"],
)
def test_the_shell_refuses_to_open(client, setup, problem: str) -> None:
    setup(client)
    origin = None if problem.startswith("opened") else f"https://{HOST}"

    with _connect(client, origin=origin) as socket:
        message = socket.receive_json()

    assert message["type"] == "error"
    assert problem in message["message"]


def test_an_unknown_container_is_refused(client) -> None:
    with _connect(client, container="sidecar") as socket:
        message = socket.receive_json()

    assert "no container named sidecar" in message["message"]


def test_a_session_relays_until_the_shell_exits_and_is_logged(client, mocker) -> None:
    streams: list[FakeStream] = []

    @asynccontextmanager
    async def fake_exec(namespace: str, pod: str, container: str):
        stream = FakeStream(
            on_input={
                b"\x00exit\r": [
                    _frame(pod_shell.STDOUT_CHANNEL, b"bye\r\n"),
                    _frame(pod_shell.ERROR_CHANNEL, EXITED),
                    None,
                ]
            }
        )
        streams.append(stream)
        yield stream

    mocker.patch.object(dashboard_shell, "exec_stream", fake_exec)

    with capture_logs() as logs, _connect(client) as socket:
        assert socket.receive_json()["type"] == "status"
        socket.send_text(json.dumps({"type": "input", "data": "exit\r"}))
        assert socket.receive_bytes() == b"bye\r\n"
        assert socket.receive_json() == {"type": "exit", "reason": "exited", "code": 0}

    assert streams[0].sent == [b"\x00exit\r"]
    events = {entry["event"]: entry for entry in logs if entry["event"].startswith("Dashboard shell")}
    assert events["Dashboard shell opened"]["container"] == "server"
    assert events["Dashboard shell closed"]["reason"] == "exited"


def _page_request(enabled: bool = True) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/dashboard/pods/idegym/p/shell",
            "headers": [],
            "query_string": b"",
            "scheme": "https",
            "server": (HOST, 443),
            "client": ("testclient", 50000),
            "app": SimpleNamespace(state=SimpleNamespace(config=_config(enabled))),
        }
    )


async def test_the_shell_page_loads_the_vendored_terminal(client) -> None:
    response = await dashboard_shell.shell_page(_page_request(), namespace="idegym", pod_name="srv-7-5d8f7c9b4-x2kqp")
    html = response.body.decode()

    assert response.status_code == 200, html
    assert "/dashboard/static/vendor/xterm/xterm.js" in html
    assert 'data-shell="/dashboard/pods/idegym/srv-7-5d8f7c9b4-x2kqp/exec?container=server"' in html
    assert "data-no-auto-refresh" in html


async def test_the_shell_page_is_404_while_disabled(client) -> None:
    response = await dashboard_shell.shell_page(_page_request(enabled=False), namespace="idegym", pod_name="p")

    assert response.status_code == 404


async def test_the_shell_page_refuses_other_pods(client) -> None:
    client.read.return_value = _pod(labels={"app": "postgres"})

    response = await dashboard_shell.shell_page(_page_request(), namespace="idegym", pod_name="postgres-0")

    assert response.status_code == 403
