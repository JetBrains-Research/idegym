import secrets
from os import environ as env

from idegym.api.auth import BasicAuth
from idegym.api.config import OTELConfig, TracingConfig
from idegym.client.client import IdeGYMClient
from idegym.utils.logging import get_logger

logger = get_logger(__name__)


def generate_test_id(length: int = 8) -> str:
    if length <= 0:
        raise ValueError("length must be positive")
    return secrets.token_hex((length + 1) // 2)[:length]


def get_test_params():
    base_url = env.get("IDEGYM_TEST_BASE_URL", "http://idegym-local.test")
    username = env.get("IDEGYM_TEST_USERNAME", "test")
    password = env.get("IDEGYM_TEST_PASSWORD", "test")

    return base_url, username, password


def create_http_client(
    name: str,
    nodes_count: int = 0,
    heartbeat_interval_in_seconds: int = 60,
    request_timeout_in_seconds: int = 60,
    **kwargs,
):
    base_url, username, password = get_test_params()

    # Remove trailing slash if present
    base_url = base_url.rstrip("/")

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
