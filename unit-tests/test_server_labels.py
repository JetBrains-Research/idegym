"""Caller-supplied labels and annotations on a server.

Two things have to hold: the metadata actually lands on every object an operator would query,
and it can never displace the managed keys the platform addresses the pod by.
"""

from uuid import uuid4

import pytest
from idegym.api.orchestrator.servers import (
    ServerKind,
    StartServerRequest,
    is_managed_annotation_key,
    is_managed_label_key,
)
from idegym.backend.utils import kubernetes_client as kc
from pydantic import ValidationError


async def _deploy(kube_clients, **kwargs):
    await kc.deploy_server(image_tag="img:latest", server_name="srv", namespace="ns", **kwargs)
    return {
        "deployment": kube_clients.apps.create_namespaced_deployment.call_args.kwargs["body"],
        "service": kube_clients.core.create_namespaced_service.call_args.kwargs["body"],
        "pdb": kube_clients.policy.create_namespaced_pod_disruption_budget.call_args.kwargs["body"],
    }


# --------------------------------------------------------------------------------------
# What lands in the cluster
# --------------------------------------------------------------------------------------


async def test_extra_labels_land_on_every_object_an_operator_queries(kube_clients) -> None:
    objects = await _deploy(kube_clients, extra_labels={"team": "research", "job": "run-42"})

    for name in ("deployment", "service", "pdb"):
        labels = objects[name].metadata.labels
        assert labels["team"] == "research", name
        assert labels["job"] == "run-42", name
    assert objects["deployment"].spec.template.metadata.labels["team"] == "research"


async def test_extra_annotations_land_on_the_pod(kube_clients) -> None:
    objects = await _deploy(kube_clients, extra_annotations={"example.com/task": "TASK-1"})

    annotations = objects["deployment"].spec.template.metadata.annotations
    assert annotations["example.com/task"] == "TASK-1"


async def test_managed_labels_survive_a_collision(kube_clients) -> None:
    """The API rejects these, but the deploy layer must not depend on that to stay correct."""
    objects = await _deploy(
        kube_clients,
        extra_labels={"app": "hijacked", "app.kubernetes.io/part-of": "somebody-else"},
    )

    labels = objects["deployment"].metadata.labels
    assert labels["app"] == "srv"
    assert labels["app.kubernetes.io/part-of"] == "idegym"


async def test_managed_annotations_survive_a_collision(kube_clients) -> None:
    objects = await _deploy(
        kube_clients,
        extra_annotations={"cluster-autoscaler.kubernetes.io/safe-to-evict": "true"},
    )

    annotations = objects["deployment"].spec.template.metadata.annotations
    assert annotations["cluster-autoscaler.kubernetes.io/safe-to-evict"] == "false"


async def test_a_caller_snapshot_annotation_never_reaches_the_pod(kube_clients) -> None:
    """With no snapshot requested, a caller's ps-name would restore a snapshot nobody recorded."""
    objects = await _deploy(
        kube_clients,
        extra_annotations={"podsnapshot.gke.io/ps-name": "someone-elses-snapshot", "example.com/task": "TASK-1"},
    )

    annotations = objects["deployment"].spec.template.metadata.annotations
    assert "podsnapshot.gke.io/ps-name" not in annotations
    assert annotations["example.com/task"] == "TASK-1"


async def test_a_requested_snapshot_wins_over_a_caller_snapshot_annotation(kube_clients) -> None:
    objects = await _deploy(
        kube_clients,
        snapshot_tag="the-recorded-one",
        extra_annotations={"podsnapshot.gke.io/ps-name": "someone-elses-snapshot"},
    )

    annotations = objects["deployment"].spec.template.metadata.annotations
    assert annotations["podsnapshot.gke.io/ps-name"] == "the-recorded-one"


async def test_the_selector_never_picks_up_caller_labels(kube_clients) -> None:
    """A selector that grew a caller label would stop matching pods started without it."""
    objects = await _deploy(kube_clients, extra_labels={"team": "research"})

    assert "team" not in objects["deployment"].spec.selector.match_labels
    assert "team" not in objects["service"].spec.selector
    assert "team" not in objects["pdb"].spec.selector.match_labels


async def test_no_extra_metadata_leaves_the_objects_as_before(kube_clients) -> None:
    objects = await _deploy(kube_clients)

    assert set(objects["deployment"].metadata.labels) == {
        "app",
        "app.kubernetes.io/component",
        "app.kubernetes.io/name",
        "app.kubernetes.io/part-of",
        "app.kubernetes.io/version",
        "idegym.jetbrains.com/snapshot-id",
    }


@pytest.mark.parametrize("server_kind", list(ServerKind))
async def test_every_key_deploy_server_sets_is_reserved_from_callers(kube_clients, server_kind) -> None:
    """The drift guard: a managed key the validators do not reserve is one a caller can take over."""
    objects = await _deploy(kube_clients, server_kind=server_kind, snapshot_id="group-1", snapshot_tag="snapshot-1")

    template = objects["deployment"].spec.template.metadata
    labels = {
        *objects["deployment"].metadata.labels,
        *template.labels,
        *objects["service"].metadata.labels,
        *objects["pdb"].metadata.labels,
    }
    assert sorted(key for key in labels if not is_managed_label_key(key)) == []
    assert sorted(key for key in template.annotations if not is_managed_annotation_key(key)) == []


# --------------------------------------------------------------------------------------
# What the request model accepts
# --------------------------------------------------------------------------------------


def _request(**kwargs) -> StartServerRequest:
    return StartServerRequest(client_id=uuid4(), image_tag="registry.test/env:latest", **kwargs)


def test_labels_and_annotations_default_to_empty() -> None:
    request = _request()

    assert (request.labels, request.annotations) == ({}, {})


@pytest.mark.parametrize(
    "reserved",
    ["app", "app.kubernetes.io/name", "app.kubernetes.io/anything", "idegym.jetbrains.com/snapshot-id"],
)
def test_a_managed_label_key_is_rejected(reserved) -> None:
    with pytest.raises(ValidationError, match="IdeGYM-managed keys"):
        _request(labels={reserved: "mine"})


def test_the_error_names_every_offending_key() -> None:
    with pytest.raises(ValidationError) as caught:
        _request(labels={"app": "a", "app.kubernetes.io/name": "b", "team": "research"})

    assert "labels may not set IdeGYM-managed keys: app, app.kubernetes.io/name" in str(caught.value)


@pytest.mark.parametrize("key", ["appliance", "team", "example.com/job", "idegym.example.com/task"])
def test_a_key_that_merely_resembles_a_managed_one_is_accepted(key) -> None:
    assert _request(labels={key: "value"}).labels == {key: "value"}


def test_an_annotation_may_use_a_managed_label_prefix() -> None:
    """Annotations carry no selector weight, so the label reservation does not apply to them."""
    assert _request(annotations={"app.kubernetes.io/notes": "long text"}).annotations


@pytest.mark.parametrize(
    "reserved",
    [
        "cluster-autoscaler.kubernetes.io/safe-to-evict",
        "podsnapshot.gke.io/ps-name",
        "podsnapshot.gke.io/anything",
        "prometheus.io/scrape",
        "prometheus.io/port",
    ],
)
def test_a_managed_annotation_key_is_rejected(reserved) -> None:
    with pytest.raises(ValidationError, match="annotations may not set IdeGYM-managed keys"):
        _request(annotations={reserved: "mine"})


def test_the_annotation_error_names_every_offending_key() -> None:
    with pytest.raises(ValidationError) as caught:
        _request(annotations={"prometheus.io/scrape": "false", "podsnapshot.gke.io/ps-name": "x", "team": "y"})

    assert "annotations may not set IdeGYM-managed keys: podsnapshot.gke.io/ps-name, prometheus.io/scrape" in str(
        caught.value
    )


@pytest.mark.parametrize("key", ["cluster-autoscaler.kubernetes.io/other", "example.com/prometheus.io", "gke.io/x"])
def test_an_annotation_that_merely_resembles_a_managed_one_is_accepted(key) -> None:
    assert _request(annotations={key: "value"}).annotations == {key: "value"}


_LONGEST_PREFIX = ".".join(["a" * 63] * 3 + ["a" * 61])  # 253 characters


@pytest.mark.parametrize(
    "labels",
    [
        {"": "value"},
        {"Not A Key": "value"},
        {"a" * 64: "value"},  # name segment over 63 characters
        {"example.com/" + "a" * 64: "value"},
        {"Example.COM/team": "value"},  # the prefix is a lowercase DNS subdomain
        {"-example.com/team": "value"},
        {"example..com/team": "value"},
        {"example.com/": "value"},
        {"/team": "value"},
        {"a/b/team": "value"},
        {"example.com/-team": "value"},
        {_LONGEST_PREFIX + "a/team": "value"},  # prefix over 253 characters
        {"team": "x" * 64},
        {"team": "-x"},
        {"team": "x."},
        {"team": "a b"},
    ],
)
def test_labels_are_held_to_the_kubernetes_syntax(labels) -> None:
    with pytest.raises(ValidationError):
        _request(labels=labels)


@pytest.mark.parametrize(
    "labels",
    [
        {"a" * 63: "value"},
        {"Team_1.x-y": ""},
        {"example.com/Team": "x" * 63},
        {"sub-1.example.com/team": "a.b_c-d"},
        {_LONGEST_PREFIX + "/team": "value"},
    ],
)
def test_labels_at_the_kubernetes_limits_are_accepted(labels) -> None:
    assert _request(labels=labels).labels == labels


def test_a_node_selector_is_held_to_the_same_key_syntax() -> None:
    """Node-selector keys are label keys, so the tightened syntax applies to them as well."""
    assert _request(node_selector={"kubernetes.io/os": "linux"}).node_selector == {"kubernetes.io/os": "linux"}
    with pytest.raises(ValidationError):
        _request(node_selector={"Kubernetes.IO/os": "linux"})


def test_annotations_are_held_to_the_total_size_limit() -> None:
    """Kubernetes caps keys and values together at 256 KiB; over it, the deploy would fail late."""
    with pytest.raises(ValidationError, match="annotations may total at most 262144 bytes"):
        _request(annotations={"example.com/a": "x" * (128 * 1024), "example.com/b": "x" * (128 * 1024)})


def test_annotation_size_is_counted_in_bytes() -> None:
    with pytest.raises(ValidationError, match="annotations may total at most"):
        _request(annotations={"example.com/notes": "é" * (128 * 1024 + 1)})


def test_annotation_values_are_not_length_limited_like_labels() -> None:
    """An annotation is where metadata too long to be a label goes."""
    assert _request(annotations={"example.com/notes": "x" * 5000}).annotations
    assert _request(annotations={"example.com/notes": "x" * (256 * 1024 - 20)}).annotations
