"""Unit tests for StartServerRequest snapshot reference modelling."""

from uuid import uuid4

import pytest
from idegym.api.orchestrator.servers import SnapshotRef, StartServerRequest
from pydantic import ValidationError

pytestmark = pytest.mark.unit

_IMAGE = "registry.example.com/server:latest"


def _request(**overrides) -> StartServerRequest:
    return StartServerRequest(client_id=uuid4(), image_tag=_IMAGE, **overrides)


def test_snapshot_ref_requires_id():
    # A tag without an id is structurally impossible: id is required on SnapshotRef.
    with pytest.raises(ValidationError):
        SnapshotRef(tag="ps-abc")


def test_snapshot_ref_id_only():
    ref = SnapshotRef(id="server-7")
    assert ref.id == "server-7"
    assert ref.tag is None


def test_request_with_snapshot_id_and_tag():
    request = _request(snapshot=SnapshotRef(id="server-7", tag="ps-abc"))
    assert request.snapshot.id == "server-7"
    assert request.snapshot.tag == "ps-abc"


def test_request_without_snapshot_is_accepted():
    request = _request()
    assert request.snapshot is None
