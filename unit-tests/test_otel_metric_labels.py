"""Label budget of exported metrics.

Prometheus fails the whole scrape once any series exceeds the job's `label_limit`, counting the
target labels it adds itself. So `target_info` carries only the service identity (traces keep the
full resource), HTTP metrics only the attributes a query filters on, and no series `otel_scope_*`.
"""

from idegym.api.config import OTELConfig
from idegym.backend.utils import otel
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource

K8S_ATTRIBUTES = {
    "k8s.pod.uid": "uid",
    "k8s.pod.name": "pod",
    "k8s.namespace.name": "ns",
    "k8s.node.name": "node",
}


def _configure(mocker):
    metrics = mocker.patch.object(otel, "configure_metrics_provider")
    tracing = mocker.patch.object(otel, "configure_tracing_provider")
    otel.configure_telemetry(OTELConfig(service_name="idegym-watcher", attributes=K8S_ATTRIBUTES))
    return metrics.call_args.args[0], tracing.call_args.args[0]


def test_metrics_resource_carries_only_the_service_identity(mocker) -> None:
    metrics_resource, _ = _configure(mocker)

    assert set(metrics_resource.attributes) == {"service.name", "service.version"}
    assert metrics_resource.attributes["service.name"] == "idegym-watcher"


def test_tracing_resource_keeps_the_kubernetes_attributes(mocker) -> None:
    _, tracing_resource = _configure(mocker)

    assert tracing_resource.attributes["service.name"] == "idegym-watcher"
    for key, value in K8S_ATTRIBUTES.items():
        assert tracing_resource.attributes[key] == value


def test_http_metrics_keep_only_queryable_attributes_and_no_scope_labels(mocker) -> None:
    reader = InMemoryMetricReader()
    reader_class = mocker.patch.object(otel, "PrometheusMetricReader", return_value=reader)
    set_provider = mocker.patch.object(otel.metrics, "set_meter_provider")
    otel.configure_metrics_provider(Resource({}))
    provider = set_provider.call_args.args[0]

    histogram = provider.get_meter("test").create_histogram("http.server.duration")
    histogram.record(
        1,
        {
            "http.method": "GET",
            "http.status_code": 200,
            "http.target": "/health",
            "http.host": "10.0.0.1:8000",
            "http.scheme": "http",
            "http.flavor": "1.1",
            "http.server_name": "pod",
            "net.host.port": 8000,
        },
    )

    assert reader_class.call_args.kwargs["scope_info_enabled"] is False
    (point,) = reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics[0].data.data_points
    assert set(point.attributes) == {"http.method", "http.status_code", "http.target"}
    provider.shutdown()
