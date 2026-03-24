import pytest
from tests.utils import create_http_client


async def async_test_orchestrator_health():
    """Test that the orchestrator health endpoint is responsive using http_client."""
    client = create_http_client(name="orchestrator-health")
    response = await client.health_check()
    assert response.status is not None, "Response does not contain 'status' field"
    assert response.status == "healthy", f"Health status is not 'healthy': {response.status}"


@pytest.mark.asyncio
async def test_orchestrator_health():
    """Test that the orchestrator health endpoint is responsive."""
    await async_test_orchestrator_health()
