"""Unit tests for StartServerRequest snapshot_tag validation."""

from uuid import uuid4

import pytest
from idegym.api.orchestrator.servers import StartServerRequest
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_IMAGE = "registry.example.com/server:latest"


def _request(**overrides) -> StartServerRequest:
    return StartServerRequest(client_id=uuid4(), image_tag=_IMAGE, **overrides)


def test_snapshot_tag_requires_snapshot_id():
    with pytest.raises(ValidationError, match="snapshot_tag can only be set together with snapshot_id"):
        _request(snapshot_tag="ps-abc")


def test_snapshot_tag_with_snapshot_id_is_accepted():
    request = _request(snapshot_id="server-7", snapshot_tag="ps-abc")
    assert request.snapshot_id == "server-7"
    assert request.snapshot_tag == "ps-abc"


def test_snapshot_id_alone_is_accepted():
    request = _request(snapshot_id="server-7")
    assert request.snapshot_id == "server-7"
    assert request.snapshot_tag is None


def test_no_snapshot_fields_is_accepted():
    request = _request()
    assert request.snapshot_id is None
    assert request.snapshot_tag is None
