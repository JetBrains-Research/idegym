"""Unit tests for the OTEL tracing env vars forwarded to spawned env-server pods.

Regression coverage for the crash-loop caused by forwarding an empty
``IDEGYM_OTEL_TRACING_ENDPOINT`` when the orchestrator has no tracing endpoint configured.
"""

from idegym.api.config import OTELConfig, TracingConfig
from idegym.orchestrator.router.server import build_otel_tracing_env

ENDPOINT = "http://tempo:4318/v1/traces"


def _names(env: list[dict]) -> set[str]:
    return {var["name"] for var in env}


def test_no_endpoint_forwards_no_tracing_env():
    # Chart default: deployment.otel.tracing is empty, so the endpoint is unset.
    assert build_otel_tracing_env(OTELConfig()) == []


def test_no_endpoint_never_emits_empty_endpoint_value():
    # The empty string is what previously crash-looped the env-server, so it must never appear.
    env = build_otel_tracing_env(OTELConfig(tracing=TracingConfig(endpoint=None)))
    assert env == []


def test_endpoint_set_forwards_all_tracing_env():
    env = build_otel_tracing_env(OTELConfig(tracing=TracingConfig(endpoint=ENDPOINT)))

    assert _names(env) == {
        "IDEGYM_OTEL_TRACING_ENDPOINT",
        "IDEGYM_OTEL_TRACING_AUTH_USERNAME",
        "IDEGYM_OTEL_TRACING_AUTH_PASSWORD",
        "IDEGYM_OTEL_TRACING_TIMEOUT",
    }


def test_endpoint_value_is_a_non_empty_string():
    (endpoint,) = [
        var
        for var in build_otel_tracing_env(OTELConfig(tracing=TracingConfig(endpoint=ENDPOINT)))
        if var["name"] == "IDEGYM_OTEL_TRACING_ENDPOINT"
    ]

    # Kubernetes requires env `value` to be a string; a raw pydantic HttpUrl is not one.
    assert isinstance(endpoint["value"], str)
    assert endpoint["value"].startswith("http://tempo:4318/v1/traces")


def test_auth_env_read_from_optional_tracing_secret():
    env = {var["name"]: var for var in build_otel_tracing_env(OTELConfig(tracing=TracingConfig(endpoint=ENDPOINT)))}

    for name, key in (
        ("IDEGYM_OTEL_TRACING_AUTH_USERNAME", "username"),
        ("IDEGYM_OTEL_TRACING_AUTH_PASSWORD", "password"),
    ):
        secret_ref = env[name]["valueFrom"]["secretKeyRef"]
        assert secret_ref == {"name": "tracing", "key": key, "optional": True}
