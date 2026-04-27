"""
Mock IDEGymRunner for testing without a real IDEGym deployment.

Drop-in replacement that returns plausible fake outputs and optionally
injects errors to exercise error-handling paths.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any
from uuid import uuid4

from examples.verl.agent_loop.idegym_runner_utils import ItemToRun

logger = logging.getLogger(__name__)


def _maybe_raise(error_rate: float, context: str) -> None:
    """Raise a randomly selected error with probability *error_rate*."""
    if random.random() >= error_rate:
        return
    exc = ConnectionError(f"[MOCK] Simulated error in {context}")
    logger.warning("[MOCK] Injecting %s in %s", type(exc).__name__, context)
    raise exc


class MockIdeGYMServer:
    """Minimal stand-in for ``idegym.client.server.IdeGYMServer``."""

    def __init__(self, server_id: str | None = None) -> None:
        self.server_id: str = server_id or str(uuid4())[:8]


_DJANGO_OK_TEMPLATE = """\
..........
Ran {n} tests in {elapsed:.3f}s

OK
"""


def _fake_test_output(pass_rate: float = 0.7, n_tests: int = 5) -> str:
    elapsed = random.uniform(0.5, 5.0)
    failures = 0 if random.random() < pass_rate else random.randint(1, max(1, n_tests // 2))
    if failures == 0:
        return _DJANGO_OK_TEMPLATE.format(n=n_tests, elapsed=elapsed)
    idx = random.randint(1, 99)
    dots = "." * (n_tests - failures)
    fs = "F" * failures
    sep = "=" * 70
    return (
        f"{dots}{fs}\n"
        f"{sep}\n"
        f"FAIL: test_example_{idx} (tests.mock_tests.MockTestCase)\n"
        f"----------------------------------------------------------------------\n"
        f"Traceback (most recent call last):\n"
        f'  File "/django/tests/mock_tests.py", line 42, in test_example_{idx}\n'
        f"    self.assertEqual(result, expected)\n"
        f"AssertionError: 'mock_output' != 'expected_output'\n"
        f"\n"
        f"----------------------------------------------------------------------\n"
        f"Ran {n_tests} tests in {elapsed:.3f}s\n"
        f"\n"
        f"FAILED (failures={failures})\n"
    )


def _fake_file_content(item: ItemToRun) -> str:
    return f"# Mock file content for {item.file_path}\ndef {item.method_name}(self):\n    pass\n"


class MockIDEGymRunner:
    """Drop-in replacement for IDEGymRunner that requires no real IDEGym infrastructure."""

    def __init__(
        self,
        error_rate: float = 0.0,
        pass_rate: float = 0.7,
        n_tests: int = 5,
        min_latency: float = 0.05,
        max_latency: float = 0.3,
        repository: str = "django",
        max_retries: int = 3,
        **kwargs: Any,
    ) -> None:
        self.error_rate = error_rate
        self.pass_rate = pass_rate
        self.n_tests = n_tests
        self.min_latency = min_latency
        self.max_latency = max_latency
        self.repository = repository
        self.max_retries = max_retries
        self.test_command_timeout = kwargs.get("test_command_timeout", 60)
        self.short_bash_cmd_timeout = kwargs.get("short_bash_cmd_timeout", 10)
        self._server_counter = 0
        logger.info("[MOCK] MockIDEGymRunner initialised (error_rate=%.2f, pass_rate=%.2f)", error_rate, pass_rate)

    async def _sleep(self) -> None:
        await asyncio.sleep(random.uniform(self.min_latency, self.max_latency))

    async def create_client(self) -> object:
        logger.info("[MOCK] create_client() called")
        await self._sleep()
        return object()  # non-None sentinel

    async def close_client(self, client) -> None:
        logger.info("[MOCK] close_client() called")
        await self._sleep()

    async def create_server(self, client=None) -> MockIdeGYMServer:
        logger.info("[MOCK] create_server() called")
        await self._sleep()
        _maybe_raise(self.error_rate, "create_server")
        self._server_counter += 1
        server = MockIdeGYMServer(server_id=f"mock-server-{self._server_counter}")
        logger.info("[MOCK] Created server %s", server.server_id)
        return server

    async def finish_server(self, server: MockIdeGYMServer) -> None:
        logger.info("[MOCK] finish_server(%s) called", server.server_id)
        await self._sleep()

    async def reset_server(self, server: MockIdeGYMServer) -> None:
        logger.info("[MOCK] reset_server(%s) called", server.server_id)
        await self._sleep()
        _maybe_raise(self.error_rate, "reset_server")

    async def run_bash(self, server: MockIdeGYMServer, command: str) -> dict[str, Any]:
        logger.info("[MOCK] run_bash(%s, %.60r...)", server.server_id, command)
        await self._sleep()
        _maybe_raise(self.error_rate, "run_bash")
        return {
            "server_id": server.server_id,
            "command": command,
            "command_output": {
                "stdout": f"[mock stdout for: {command[:80]}]",
                "stderr": "",
                "exit_code": 0,
            },
        }

    async def run_tests(self, server: MockIdeGYMServer, item: ItemToRun, do_edit: bool = True) -> dict[str, Any]:
        logger.info("[MOCK] run_tests(%s, dp_id=%s, do_edit=%s)", server.server_id, item.dp_id, do_edit)
        time_start = time.perf_counter()
        await self._sleep()
        _maybe_raise(self.error_rate, "run_tests")

        test_output = _fake_test_output(self.pass_rate, self.n_tests)
        edited_file = _fake_file_content(item)

        elapsed = time.perf_counter() - time_start
        return {
            "datapoint": item,
            "test_output": test_output,
            "edited_file": edited_file,
            "time_test": elapsed * 0.7,
            "time_cat": elapsed * 0.1,
            "time_edit": elapsed * 0.2,
            "time_total": elapsed,
            "server_id": server.server_id,
        }

    async def edit_file(self, server: MockIdeGYMServer, item: ItemToRun) -> dict[str, Any]:
        logger.info("[MOCK] edit_file(%s, dp_id=%s)", server.server_id, item.dp_id)
        await self._sleep()
        _maybe_raise(self.error_rate, "edit_file")
        return {
            "server_id": server.server_id,
            "edited_file": _fake_file_content(item),
        }
