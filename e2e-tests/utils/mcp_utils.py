import asyncio
import json
import time
from typing import Any, Optional

import httpx
from fastmcp import Client
from idegym.api.orchestrator.mcp import MCPToolName
from idegym.api.orchestrator.operations import AsyncOperationStatus
from utils.idegym_utils import get_test_params


def create_mcp_client(timeout: float = 600.0) -> Client:
    base_url, username, password = get_test_params()
    mcp_url = f"{base_url.rstrip('/')}/mcp"
    return Client(
        mcp_url,
        auth=httpx.BasicAuth(username=username, password=password),
        timeout=timeout,
    )


async def wait_for_mcp_operation(
    mcp: Client,
    operation_id: int,
    timeout: float = 600.0,
    poll_interval: float = 1.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout

    while True:
        result = await mcp.call_tool(MCPToolName.GET_OPERATION_STATUS, {"operation_id": operation_id})
        status = result.structured_content
        operation_status = AsyncOperationStatus(status["status"])

        if operation_status is AsyncOperationStatus.SUCCEEDED:
            return status

        if operation_status in (AsyncOperationStatus.FAILED, AsyncOperationStatus.CANCELLED):
            raise AssertionError(f"MCP operation {operation_id} ended with {operation_status}: {status}")

        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for MCP operation {operation_id}. Last status: {status}")

        await asyncio.sleep(poll_interval)


def parse_operation_result(status: dict[str, Any]) -> dict[str, Any]:
    result: Optional[str] = status.get("result")
    if result is None:
        raise AssertionError(f"Operation {status.get('id')} succeeded without a result: {status}")
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError as ex:
        raise AssertionError(f"Operation {status.get('id')} result is not JSON: {result}") from ex
    if not isinstance(parsed, dict):
        raise AssertionError(f"Operation {status.get('id')} result is not an object: {parsed!r}")
    return parsed


def parse_forwarded_body(status: dict[str, Any]) -> dict[str, Any]:
    forward_response = parse_operation_result(status)
    body = forward_response.get("body")
    if body is None:
        raise AssertionError(f"Forwarded operation {status.get('id')} result has no body: {forward_response}")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as ex:
        raise AssertionError(f"Forwarded operation {status.get('id')} body is not JSON: {body}") from ex
    if not isinstance(parsed, dict):
        raise AssertionError(f"Forwarded operation {status.get('id')} body is not an object: {parsed!r}")
    return parsed
