"""Unit tests for IdeGYMServer client — forward() method and plugin operations loop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from idegym.client.operations.utils import PollingConfig
from idegym.client.server import IdeGYMServer


def _make_server(**kwargs) -> IdeGYMServer:
    """Return an IdeGYMServer with a minimal mock HTTPUtils."""
    http_utils = MagicMock()
    http_utils.validate_client_id.side_effect = lambda cid: cid
    http_utils.base_url = "http://localhost:8000"
    defaults = dict(server_id=1, http_utils=http_utils)
    defaults.update(kwargs)
    return IdeGYMServer(**defaults)


# ---------------------------------------------------------------------------
# IdeGYMServer.forward()
# ---------------------------------------------------------------------------


def test_forward_delegates_to_forwarding_operations():
    """forward() passes all arguments through to _forwarding.forward_request."""
    server = _make_server(server_id=7, client_id=None)
    server._forwarding = MagicMock()
    server._forwarding.forward_request = AsyncMock(return_value={"ok": True})

    polling = PollingConfig()
    result = asyncio.run(
        server.forward(
            method="GET",
            path="pycharm/health",
            body=None,
            request_timeout=30,
            polling_config=polling,
        )
    )

    assert result == {"ok": True}
    server._forwarding.forward_request.assert_called_once_with(
        method="GET",
        server_id=7,
        path="pycharm/health",
        body=None,
        client_id=None,
        request_timeout=30,
        polling_config=polling,
    )


def test_forward_uses_instance_polling_config_when_none_given():
    """forward() falls back to self.polling_config when no override is provided."""
    polling = PollingConfig(wait_timeout_in_sec=120)
    server = _make_server(server_id=3, polling_config=polling)
    server._forwarding = MagicMock()
    server._forwarding.forward_request = AsyncMock(return_value={})

    asyncio.run(server.forward(method="POST", path="some/endpoint"))

    _call_kwargs = server._forwarding.forward_request.call_args.kwargs
    assert _call_kwargs["polling_config"] is polling


def test_forward_passes_body():
    """forward() passes a Pydantic body model through unchanged."""
    from pydantic import BaseModel

    class _Body(BaseModel):
        value: int

    body = _Body(value=99)
    server = _make_server()
    server._forwarding = MagicMock()
    server._forwarding.forward_request = AsyncMock(return_value={})

    asyncio.run(server.forward(method="POST", path="tools/bash", body=body))

    _call_kwargs = server._forwarding.forward_request.call_args.kwargs
    assert _call_kwargs["body"] is body


# ---------------------------------------------------------------------------
# Plugin operations loop — server.pycharm (discovered via entry_points)
# ---------------------------------------------------------------------------


def test_pycharm_operations_attached_when_plugin_discovered():
    """server.pycharm is set to a PycharmClientOperations instance via entry_point discovery."""
    from idegym.plugins.pycharm.client import PycharmClientOperations

    server = _make_server(server_id=5)
    assert hasattr(server, "pycharm"), "server.pycharm attribute not set"
    assert isinstance(server.pycharm, PycharmClientOperations)


def test_pycharm_operations_uses_correct_server_id():
    """Plugin ops object stores the server_id from IdeGYMServer.__init__."""
    server = _make_server(server_id=42)
    assert server.pycharm._server_id == 42


def test_pycharm_operations_uses_correct_client_id():
    """Plugin ops object stores the client_id from IdeGYMServer.__init__."""
    from uuid import uuid4

    cid = uuid4()
    server = _make_server(client_id=cid)
    assert server.pycharm._client_id == cid


def test_image_only_plugins_not_attached_as_client_ops():
    """Image-only plugins (no idegym.plugins.client entry_point) do not produce server attributes."""
    server = _make_server()
    # "base-system" only has an idegym.plugins.image entry_point, not .client
    assert not hasattr(server, "base-system")
    assert not hasattr(server, "basesystem")


# ---------------------------------------------------------------------------
# Plugin ops — independence, forwarding object, and error resilience
# ---------------------------------------------------------------------------


def test_pycharm_ops_forwarding_field_matches_server_forwarding():
    """server.pycharm._forward is the same forwarding object as server._forwarding."""
    server = _make_server(server_id=1)
    assert server.pycharm._forward is server._forwarding


def test_two_server_instances_get_independent_plugin_ops():
    """Each IdeGYMServer instance creates its own plugin ops objects."""
    server_a = _make_server(server_id=10)
    server_b = _make_server(server_id=20)
    assert server_a.pycharm is not server_b.pycharm
    assert server_a.pycharm._server_id == 10
    assert server_b.pycharm._server_id == 20


def test_client_init_survives_entry_point_load_error(monkeypatch):
    """IdeGYMServer.__init__ swallows all exceptions from idegym.plugins.client entry_point loading."""
    import importlib.metadata as meta

    original_entry_points = meta.entry_points

    def _patched_entry_points(group=None, **kwargs):
        if group == "idegym.plugins.client":

            class _BadEP:
                name = "bad-plugin"

                def load(self):
                    raise ImportError("Simulated missing optional dependency")

            return [_BadEP()]
        return original_entry_points(group=group, **kwargs)

    monkeypatch.setattr(meta, "entry_points", _patched_entry_points)
    # Should not raise — the except-Exception block in __init__ swallows all errors
    server = _make_server()
    # The bad entry_point must not have created an attribute (neither hyphenated nor underscored)
    assert not hasattr(server, "bad-plugin")
    assert not hasattr(server, "bad_plugin")


def test_hyphenated_entry_point_name_becomes_underscore_attribute(monkeypatch):
    """Entry point names with hyphens are mapped to underscored attributes for valid Python syntax."""
    import importlib.metadata as meta

    original_entry_points = meta.entry_points

    class _FakeOps:
        def __init__(self, forward, server_id, client_id, polling_config):
            pass

    def _patched_entry_points(group=None, **kwargs):
        if group == "idegym.plugins.client":

            class _HyphenEP:
                name = "my-plugin"

                def load(self):
                    return _FakeOps

            return [_HyphenEP()]
        return original_entry_points(group=group, **kwargs)

    monkeypatch.setattr(meta, "entry_points", _patched_entry_points)
    server = _make_server()
    assert hasattr(server, "my_plugin"), "hyphenated name should be accessible as my_plugin"
    assert not hasattr(server, "my-plugin"), "raw hyphenated name should not be set as an attribute"
    assert isinstance(server.my_plugin, _FakeOps)


# ---------------------------------------------------------------------------
# IdeGYMServer.capabilities()
# ---------------------------------------------------------------------------


def test_capabilities_delegates_to_server_operations():
    """capabilities() delegates to self.server.capabilities with server_id and client_id."""
    from uuid import uuid4

    from idegym.api.capabilities import CapabilitiesResponse

    cid = uuid4()
    server = _make_server(server_id=7, client_id=cid)
    server.server = MagicMock()
    server.server.capabilities = AsyncMock(return_value=CapabilitiesResponse(plugins=["tools", "rewards"]))

    result = asyncio.run(server.capabilities())

    assert result.plugins == ["tools", "rewards"]
    server.server.capabilities.assert_called_once_with(server_id=7, client_id=cid)


def test_capabilities_returns_capabilities_response():
    """capabilities() returns a typed CapabilitiesResponse, not a raw dict."""
    from idegym.api.capabilities import CapabilitiesResponse

    server = _make_server(server_id=1)
    server.server = MagicMock()
    server.server.capabilities = AsyncMock(return_value=CapabilitiesResponse(plugins=["tools"]))

    result = asyncio.run(server.capabilities())

    assert isinstance(result, CapabilitiesResponse)


def test_server_operations_capabilities_calls_correct_url():
    """ServerOperations.capabilities() calls GET /api/idegym-servers/{id}/capabilities?client_id=..."""
    from uuid import uuid4

    from idegym.api.capabilities import CapabilitiesResponse
    from idegym.client.operations.servers import ServerOperations

    cid = uuid4()
    http_utils = MagicMock()
    http_utils.validate_client_id.return_value = cid
    http_utils.make_request = AsyncMock(return_value={"plugins": ["tools", "rewards"]})

    ops = ServerOperations(utils=http_utils, project=MagicMock())
    result = asyncio.run(ops.capabilities(server_id=42, client_id=cid))

    assert isinstance(result, CapabilitiesResponse)
    assert result.plugins == ["tools", "rewards"]
    http_utils.make_request.assert_called_once_with(
        "GET",
        f"/api/idegym-servers/42/capabilities?client_id={cid}",
    )
