"""Tracing is opt-in.

The failure this guards against is silent: a client built with no OTEL configuration used to
export spans to a hardcoded remote collector, which nobody sees until they look at egress.
"""

import pytest
from idegym.api.auth import BasicAuth
from idegym.api.config import OTELConfig, TracingConfig
from idegym.client.client import IdeGYMClient

CREDENTIALS = {"IDEGYM_AUTH_USERNAME": "user", "IDEGYM_AUTH_PASSWORD": "secret"}


@pytest.fixture
def instrumented(mocker):
    """Capture the config the client hands to ``instrument`` without touching OTEL itself."""
    return mocker.patch("idegym.client.client.instrument")


def _set_environment(monkeypatch, **values) -> None:
    for name in (
        "IDEGYM_OTEL_TRACING_ENDPOINT",
        "IDEGYM_OTEL_SERVICE_NAME",
        "IDEGYM_OTEL_TRACING_TIMEOUT",
        "IDEGYM_OTEL_TRACING_AUTH_USERNAME",
        "IDEGYM_OTEL_TRACING_AUTH_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**CREDENTIALS, **values}.items():
        monkeypatch.setenv(name, value)


def test_tracing_is_off_without_any_configuration(monkeypatch, instrumented) -> None:
    _set_environment(monkeypatch)

    IdeGYMClient(orchestrator_url="idegym.test", name="c", namespace="idegym")

    config = instrumented.call_args.kwargs["config"]
    assert config.tracing.endpoint is None
    assert config.tracing.enabled is False


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_endpoint_variable_does_not_enable_tracing(monkeypatch, instrumented, blank) -> None:
    _set_environment(monkeypatch, IDEGYM_OTEL_TRACING_ENDPOINT=blank)

    IdeGYMClient(orchestrator_url="idegym.test", name="c", namespace="idegym")

    assert instrumented.call_args.kwargs["config"].tracing.enabled is False


def test_an_endpoint_from_the_environment_enables_tracing(monkeypatch, instrumented) -> None:
    _set_environment(monkeypatch, IDEGYM_OTEL_TRACING_ENDPOINT="https://collector.internal/v1/traces")

    IdeGYMClient(orchestrator_url="idegym.test", name="c", namespace="idegym")

    config = instrumented.call_args.kwargs["config"]
    assert config.tracing.enabled is True
    assert str(config.tracing.endpoint) == "https://collector.internal/v1/traces"


def test_an_explicit_config_still_wins(monkeypatch, instrumented) -> None:
    _set_environment(monkeypatch, IDEGYM_OTEL_TRACING_ENDPOINT="https://ignored.example/v1/traces")
    explicit = OTELConfig(
        service_name="explicit",
        tracing=TracingConfig(endpoint="https://chosen.internal/v1/traces", auth=BasicAuth()),
    )

    IdeGYMClient(orchestrator_url="idegym.test", name="c", namespace="idegym", otel_config=explicit)

    assert instrumented.call_args.kwargs["config"] is explicit
