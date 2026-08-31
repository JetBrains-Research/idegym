"""The loop-owning facade.

The properties that matter are all about loops and threads, so these tests drive it from
several of each rather than mocking the concurrency away. The underlying ``IdeGYMClient`` is
stubbed: what is under test is the marshalling, not the HTTP.
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from idegym.client import shared as shared_module
from idegym.client.shared import SharedIdeGYMClient, _LoopThread


class _FakeClient:
    """Records the loop and thread each call ran on."""

    instances: list["_FakeClient"] = []

    def __init__(self, **arguments):
        self.arguments = arguments
        self.entered = False
        self.exited_with = None
        self.creation_thread = threading.current_thread().name
        _FakeClient.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exception_type, exception, traceback):
        self.exited_with = (exception_type, exception, traceback)
        return False

    async def where_am_i(self) -> tuple[int, str]:
        await asyncio.sleep(0)
        return id(asyncio.get_running_loop()), threading.current_thread().name

    async def boom(self):
        raise ValueError("from inside the loop")


@pytest.fixture(autouse=True)
def fake_client(mocker):
    _FakeClient.instances.clear()
    mocker.patch.object(shared_module, "IdeGYMClient", _FakeClient)
    return _FakeClient


def _shared() -> SharedIdeGYMClient:
    return SharedIdeGYMClient(orchestrator_url="idegym.test", name="c", namespace="idegym")


# --------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------


def test_entering_constructs_and_registers_on_the_owned_loop(fake_client) -> None:
    with _shared() as handle:
        assert handle.client.entered is True
        assert handle.client.creation_thread.startswith("idegym-client-loop")
        assert handle.client.arguments["name"] == "c"


def test_exiting_deregisters_and_stops_the_loop() -> None:
    handle = _shared()
    with handle:
        client = handle.client
        loop = handle._loop

    assert client.exited_with == (None, None, None)
    assert loop._loop.is_closed()


def test_an_exception_in_the_body_reaches_the_client_teardown() -> None:
    handle = _shared()
    with pytest.raises(ValueError, match="body failed"), handle:
        client = handle.client
        raise ValueError("body failed")

    assert client.exited_with[0] is ValueError


def test_a_failed_registration_does_not_leak_the_loop_thread(mocker) -> None:
    mocker.patch.object(_FakeClient, "__aenter__", side_effect=RuntimeError("registration refused"))
    before = {thread.name for thread in threading.enumerate()}

    with pytest.raises(RuntimeError, match="registration refused"), _shared():
        pass

    assert not {name for name in {t.name for t in threading.enumerate()} - before if "idegym-client-loop" in name}


def test_using_the_handle_before_entering_is_an_error() -> None:
    handle = _shared()

    with pytest.raises(RuntimeError, match="not started"):
        _ = handle.client
    with pytest.raises(RuntimeError, match="not started"):
        handle.run(lambda client: client.where_am_i())


# --------------------------------------------------------------------------------------
# Marshalling
# --------------------------------------------------------------------------------------


def test_calls_run_on_the_owned_loop_not_the_callers_thread() -> None:
    with _shared() as handle:
        _loop_id, thread_name = handle.run(lambda client: client.where_am_i())

    assert thread_name.startswith("idegym-client-loop")
    assert thread_name != threading.current_thread().name


def test_an_exception_inside_the_loop_surfaces_to_the_caller() -> None:
    with _shared() as handle, pytest.raises(ValueError, match="from inside the loop"):
        handle.run(lambda client: client.boom())


def test_many_threads_share_one_registration() -> None:
    with _shared() as handle, ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: handle.run(lambda client: client.where_am_i()), range(32)))

    assert len({loop_id for loop_id, _ in results}) == 1
    assert len(_FakeClient.instances) == 1


def test_a_caller_running_its_own_loop_can_still_use_the_handle() -> None:
    """One loop per sandbox is the shape that could not share a registration before."""
    with _shared() as handle:
        owned_loop_id, _ = handle.run(lambda client: client.where_am_i())

        async def from_another_loop():
            return await asyncio.to_thread(handle.run, lambda client: client.where_am_i())

        seen_from_other_loop, _ = asyncio.run(from_another_loop())

    assert seen_from_other_loop == owned_loop_id


def test_submit_returns_before_the_call_completes() -> None:
    with _shared() as handle:
        future = handle.submit(lambda client: client.where_am_i())

        assert future.result(timeout=5)[1].startswith("idegym-client-loop")


# --------------------------------------------------------------------------------------
# The loop thread itself
# --------------------------------------------------------------------------------------


def test_loop_thread_creates_the_awaitable_on_its_own_loop() -> None:
    loop_thread = _LoopThread(name="idegym-client-loop-test")
    captured = {}

    async def record():
        captured["loop"] = id(asyncio.get_running_loop())

    def factory():
        captured["factory_loop_running"] = _running_loop_id()
        return record()

    try:
        loop_thread.submit(factory).result(timeout=5)
    finally:
        loop_thread.close()

    assert captured["factory_loop_running"] == captured["loop"]


def _running_loop_id():
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return None
