"""Supplying or configuring the client's HTTP stack.

The behaviour worth pinning is ownership: a client IdeGYM builds is closed on exit, and one
handed in is not — closing somebody else's pooled client out from under them is the failure
this feature has to avoid while removing the need to reach into private attributes.
"""

import httpx
import pytest
from idegym.client.client import IdeGYMClient

CREDENTIALS = {"IDEGYM_AUTH_USERNAME": "user", "IDEGYM_AUTH_PASSWORD": "secret"}


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.delenv("IDEGYM_OTEL_TRACING_ENDPOINT", raising=False)
    for name, value in CREDENTIALS.items():
        monkeypatch.setenv(name, value)


def _build(**kwargs) -> IdeGYMClient:
    return IdeGYMClient(orchestrator_url="idegym.test", name="c", namespace="idegym", **kwargs)


def test_a_supplied_transport_is_the_one_used() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    client = _build(transport=transport)

    assert client._http_client._transport is transport


def test_supplied_limits_reach_the_pool() -> None:
    client = _build(limits=httpx.Limits(max_connections=7, max_keepalive_connections=3))

    pool = client._http_client._transport._pool
    assert pool._max_connections == 7
    assert pool._max_keepalive_connections == 3


def test_a_supplied_client_is_used_verbatim() -> None:
    supplied = httpx.AsyncClient(base_url="http://elsewhere.test")

    client = _build(http_client=supplied)

    assert client._http_client is supplied
    assert str(supplied.base_url) == "http://elsewhere.test"


@pytest.mark.parametrize(
    "conflicting",
    [
        {"transport": httpx.MockTransport(lambda request: httpx.Response(200))},
        {"limits": httpx.Limits(max_connections=1)},
    ],
)
def test_configuring_a_client_you_also_supplied_is_rejected(conflicting) -> None:
    with pytest.raises(ValueError, match="http_client"):
        _build(http_client=httpx.AsyncClient(base_url="http://elsewhere.test"), **conflicting)


@pytest.fixture
def unregistered_exit(mocker):
    """Let ``__aexit__`` run without a registration behind it."""
    mocker.patch.object(IdeGYMClient, "_stop_client", mocker.AsyncMock())


async def test_a_client_idegym_built_is_closed_on_exit(unregistered_exit) -> None:
    client = _build()

    await client.__aexit__(None, None, None)

    assert client._http_client.is_closed


async def test_a_supplied_client_survives_exit(unregistered_exit) -> None:
    supplied = httpx.AsyncClient(base_url="http://idegym.test")
    client = _build(http_client=supplied)

    await client.__aexit__(None, None, None)

    assert not supplied.is_closed
    await supplied.aclose()
