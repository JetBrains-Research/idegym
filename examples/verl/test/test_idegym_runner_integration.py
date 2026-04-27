"""
Integration tests for IDEGymRunner against a real IDEGym deployment.

These tests require:
  - The ``idegym`` and ``kubernetes_asyncio`` packages to be installed
  - IDEGYM_AUTH_USERNAME / IDEGYM_AUTH_PASSWORD env vars (or .env file)
  - A reachable IDEGym orchestrator (ORCHESTRATOR_URL)

Run:
    PYTHONPATH=. pytest examples/verl/test/test_idegym_runner_integration.py -v -s
"""

from __future__ import annotations

import asyncio
import time

import pytest

from examples.verl.agent_loop.idegym_runner import IDEGymRunner
from examples.verl.agent_loop.idegym_runner_utils import ItemToRun

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def runner() -> IDEGymRunner:
    """A real IDEGymRunner instance (reads credentials from env / .env)."""
    return IDEGymRunner()


@pytest.fixture(scope="module")
def client_and_server(runner):
    """Create a real client + server; tear down after the module finishes."""
    async def _setup():
        client = await runner.create_client()
        server = await runner.create_server(client)
        return client, server

    async def _teardown(client, server):
        await runner.finish_server(server)
        await runner.close_client(client)

    client, server = asyncio.run(_setup())
    yield client, server
    asyncio.run(_teardown(client, server))


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------

class TestConnection:
    def test_create_client(self, runner):
        """health_check must pass and client must be returned."""
        async def _run():
            client = await runner.create_client()
            assert client is not None
            await runner.close_client(client)

        asyncio.run(_run())

    def test_create_server(self, runner):
        """A server must start and receive a non-empty server_id."""
        async def _run():
            client = await runner.create_client()
            server = await runner.create_server(client)
            assert server is not None
            assert server.server_id  # non-empty
            await runner.finish_server(server)
            await runner.close_client(client)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Command execution tests
# ---------------------------------------------------------------------------

class TestRunBash:
    def test_echo_hello(self, client_and_server, runner):
        """The simplest possible command: echo hello."""
        _, server = client_and_server

        async def _run():
            return await runner.run_bash(server, "echo hello")

        result = asyncio.run(_run())

        assert result["server_id"] == server.server_id
        assert result["command"] == "echo hello"
        assert result["command_output"]["exit_code"] == 0
        assert "hello" in result["command_output"]["stdout"]

    def test_pwd(self, client_and_server, runner):
        """pwd should return a non-empty path."""
        _, server = client_and_server

        async def _run():
            return await runner.run_bash(server, "pwd")

        result = asyncio.run(_run())

        assert result["command_output"]["exit_code"] == 0
        assert result["command_output"]["stdout"].strip()  # non-empty path

    def test_exit_code_nonzero_on_failure(self, client_and_server, runner):
        """A failing command must report a non-zero exit code."""
        _, server = client_and_server

        async def _run():
            return await runner.run_bash(server, "false")

        result = asyncio.run(_run())

        assert result["command_output"]["exit_code"] != 0

    def test_stderr_captured(self, client_and_server, runner):
        """stderr output must be captured."""
        _, server = client_and_server

        async def _run():
            return await runner.run_bash(server, "echo error_msg >&2")

        result = asyncio.run(_run())

        assert "error_msg" in result["command_output"]["stderr"]


# ---------------------------------------------------------------------------
# Test execution tests
# ---------------------------------------------------------------------------

class TestRunTests:
    def _make_item(self) -> ItemToRun:
        return ItemToRun(
            idx=0,
            dp_id="integration-test-dp",
            file_path="django/db/models/base.py",
            replace_content="    def save(self, *args, **kwargs):\n        super().save(*args, **kwargs)\n",
            method_name="save",
            start_line=747,
            end_line=760,
            tests=["basic.tests"],
        )

    def test_run_tests_returns_output(self, client_and_server, runner):
        """run_tests must return a dict with test_output and timing keys."""
        _, server = client_and_server
        item = self._make_item()

        async def _run():
            return await runner.run_tests(server, item, do_edit=False)

        result = asyncio.run(_run())

        for key in ("test_output", "edited_file", "time_test", "time_total", "server_id"):
            assert key in result, f"missing key: {key}"
        assert result["server_id"] == server.server_id
        assert isinstance(result["test_output"], str)
        assert len(result["test_output"]) > 0


# ---------------------------------------------------------------------------
# Notebook-style smoke test (create client/server, run bash, teardown)
# ---------------------------------------------------------------------------

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"


def _print_result(name: str, ok: bool, elapsed: float) -> None:
    status = f"{GREEN}PASSED{RESET}" if ok else f"{RED}FAILED{RESET}"
    print(f"{name:<20} {status}  ({elapsed:.3f}s)")


def test_idegym_smoke():
    """End-to-end smoke: create client+server, run echo hello, verify, teardown."""
    results: list[tuple[str, bool, float]] = []

    async def _step(name: str, coro):
        t0 = time.perf_counter()
        try:
            value = await coro
            results.append((name, True, time.perf_counter() - t0))
            return value
        except Exception as e:
            results.append((name, False, time.perf_counter() - t0))
            raise e

    async def _run():
        runner = IDEGymRunner()

        client = await _step("create_client", runner.create_client())
        assert client is not None, "client must not be None"

        server = await _step("create_server", runner.create_server(client))
        assert server is not None
        assert server.server_id, "server_id must be non-empty"

        try:
            result = await _step("run_bash", runner.run_bash(server, "echo hello"))
            assert result["command_output"]["exit_code"] == 0
            assert "hello" in result["command_output"]["stdout"]
        finally:
            await _step("finish_server", runner.finish_server(server))
            await _step("close_client", runner.close_client(client))

    try:
        asyncio.run(_run())
    finally:
        print("\n" + 70 * "-")
        for name, ok, elapsed in results:
            _print_result(name, ok, elapsed)
        total = sum(e for _, _, e in results)
        print(f"{'TOTAL':<20}         {total:.3f}s")
