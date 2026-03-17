from pathlib import Path
from socket import gethostname
from typing import Dict, List, Optional

import psutil
from idegym.api.config import OTELConfig
from idegym.utils import __version__ as service_version
from idegym.utils.logging import get_logger
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.attributes.service_attributes import SERVICE_NAME, SERVICE_VERSION

logger = get_logger(__name__)

system_metrics_config: Dict[str, Optional[List[str]]] = {
    # process
    "process.context_switches": ["involuntary", "voluntary"],
    "process.cpu.time": ["user", "system"],
    "process.cpu.utilization": ["user", "system"],
    "process.memory.usage": None,
    "process.memory.virtual": None,
    "process.open_file_descriptor.count": None,
    "process.runtime.context_switches": ["involuntary", "voluntary"],
    "process.runtime.cpu.utilization": None,
    "process.runtime.cpu.time": ["user", "system"],
    "process.runtime.gc_count": None,
    "process.runtime.memory": ["rss", "vms"],
    "process.runtime.thread_count": None,
    "process.thread.count": None,
    # system
    "system.cpu.time": [],
    "system.cpu.utilization": [],
    "system.disk.io": ["read", "write"],
    "system.disk.operations": ["read", "write"],
    "system.disk.time": ["read", "write"],
    "system.memory.usage": ["used", "free", "cached"],
    "system.memory.utilization": ["used", "free", "cached"],
    "system.network.connections": ["family", "type"],
    "system.network.dropped.packets": ["transmit", "receive"],
    "system.network.errors": ["transmit", "receive"],
    "system.network.io": ["transmit", "receive"],
    "system.network.packets": ["transmit", "receive"],
    "system.swap.usage": ["used", "free"],
    "system.swap.utilization": ["used", "free"],
    "system.thread_count": None,
}

if psutil.MACOS:
    # see https://github.com/giampaolo/psutil/issues/1219
    system_metrics_config.pop("system.network.connections")

if psutil.LINUX:
    vmstat = Path(psutil.PROCFS_PATH) / "vmstat"
    if not vmstat.exists():  # Handle environments with synthetic /proc
        system_metrics_config.pop("system.swap.usage")
        system_metrics_config.pop("system.swap.utilization")


def configure_telemetry(config: OTELConfig):
    service_name = config.service_name or gethostname()
    resource = Resource.create(
        attributes={
            SERVICE_NAME: service_name,
            SERVICE_VERSION: service_version,
            **config.attributes,
        },
    )
    configure_metrics_provider(resource)
    configure_tracing_provider(resource, config)


def configure_metrics_provider(resource: Resource):
    reader = PrometheusMetricReader()
    provider = MeterProvider(
        metric_readers=[reader],
        resource=resource,
    )

    metrics.set_meter_provider(provider)
    logger.info("Metrics provider configured with Prometheus reader!")


def configure_tracing_provider(resource: Resource, config: OTELConfig):
    if not config.tracing.enabled:
        logger.info("No endpoint provided, skipping tracing export configuration...")
        return

    provider = TracerProvider(
        sampler=None,
        resource=resource,
    )
    trace.set_tracer_provider(provider)

    headers = {"Authorization": f"Basic {token}"} if (token := config.tracing.auth.base64) else {}

    exporter = OTLPSpanExporter(
        endpoint=config.tracing.endpoint,
        timeout=config.tracing.timeout,
        headers=headers,
    )

    processor = BatchSpanProcessor(exporter)
    trace.get_tracer_provider().add_span_processor(processor)
    logger.info(f"Sending traces to: {config.tracing.endpoint}")
