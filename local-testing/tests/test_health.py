"""Test orchestrator health endpoint."""

import pytest

from .utils import create_http_client


@pytest.mark.asyncio
async def test_orchestrator_health():
    """Test that the orchestrator health endpoint is responsive."""
    async with create_http_client(name="orchestrator-health") as client:
        response = await client.health_check()
        assert response.status is not None, "Response does not contain 'status' field"
        assert response.status == "healthy", f"Health status is not 'healthy': {response.status}"
