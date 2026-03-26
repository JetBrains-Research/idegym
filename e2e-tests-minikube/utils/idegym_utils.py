import os
import secrets

from idegym.api.auth import BasicAuth
from idegym.api.config import OTELConfig, TracingConfig
from idegym.client.client import IdeGYMClient
from idegym.utils.logging import get_logger

logger = get_logger(__name__)


def generate_test_id(length: int = 8) -> str:
    """
    Return a short hexadecimal test identifier.

    The default value keeps names compact while still providing enough entropy
    for parallel test runs.
    """
    if length <= 0:
        raise ValueError("length must be positive")
    return secrets.token_hex((length + 1) // 2)[:length]


def get_test_params():
    """Get the test parameters from environment variables."""
    base_url = os.environ.get("IDEGYM_TEST_BASE_URL", "http://idegym-local.test")
    username = os.environ.get("IDEGYM_TEST_USERNAME", "test")
    password = os.environ.get("IDEGYM_TEST_PASSWORD", "test")

    return base_url, username, password


def create_http_client(
    name: str,
    nodes_count: int = 0,
    heartbeat_interval_in_seconds: int = 60,
    request_timeout_in_seconds: int = 60,
    **kwargs,
):
    """
    Create an HTTP client for testing.

    Args:
        name: Name identifying the client (used for quota assignment)
        nodes_count: Number of nodes to request from the orchestrator (default: 0)
        heartbeat_interval_in_seconds: Interval in seconds for sending heartbeats (default: 60)
        request_timeout_in_seconds: Default timeout in seconds for every operation related to a http client (default: 60)
        client_id: If provided, the client will work as a client with the given ID, but will not send heartbeats
        **kwargs: Additional keyword arguments to pass to IdeGYMClient constructor
    """
    base_url, username, password = get_test_params()

    # Remove trailing slash if present
    if base_url.endswith("/"):
        base_url = base_url[:-1]

    # Create HTTP client with all parameters
    return IdeGYMClient(
        orchestrator_url=base_url,
        auth=BasicAuth(
            username=username,
            password=password,
        ),
        heartbeat_interval_in_seconds=heartbeat_interval_in_seconds,
        request_timeout_in_seconds=request_timeout_in_seconds,
        name=name,
        namespace="idegym-local",
        nodes_count=nodes_count,
        otel_config=OTELConfig(
            service_name=None,
            tracing=TracingConfig(),
        ),
        **kwargs,
    )
