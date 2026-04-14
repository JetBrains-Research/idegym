from typing import Optional
from uuid import uuid4

from httpx import AsyncClient
from idegym.api.config import OTELConfig
from idegym.utils import __version__ as service_version
from idegym.utils.logging import get_logger

try:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    http_client_instrumentor: Optional[HTTPXClientInstrumentor] = HTTPXClientInstrumentor()
except ImportError:
    HTTPXClientInstrumentor = None
    http_client_instrumentor = None

logger = get_logger(__name__)


def generate_service_name() -> str:
    return f"idegym-client-{uuid4()}"


def instrument(
    client: AsyncClient,
    config: OTELConfig,
):
    if not config.tracing.enabled:
        logger.info("Tracing not enabled. Skipping instrumentation.")
        return

    if not http_client_instrumentor:
        logger.warning("OpenTelemetry `httpx` instrumentation libraries not available! Skipping instrumentation...")
        return

    service_name = config.service_name or generate_service_name()
    endpoint = config.tracing.endpoint
    timeout = config.tracing.timeout
    headers = {"Authorization": f"Basic {credential}"} if (credential := config.tracing.auth.base64) else {}

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.semconv.attributes.service_attributes import SERVICE_NAME, SERVICE_VERSION

        resource = Resource.create(
            attributes={
                SERVICE_NAME: service_name,
                SERVICE_VERSION: service_version,
            },
        )
        provider = TracerProvider(
            sampler=None,
            resource=resource,
        )
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            timeout=timeout,
            headers=headers,
        )
        processor = BatchSpanProcessor(exporter)
        provider.add_span_processor(processor)
        http_client_instrumentor.instrument_client(
            client=client,
            tracer_provider=provider,
        )
        logger.info(f"Sending traces as: {service_name}")
        logger.info(f"Sending traces to: {endpoint}")
    except ImportError:
        logger.warning("OpenTelemetry SDK not available! Skipping instrumentation...")


def uninstrument(
    client: AsyncClient,
    config: OTELConfig,
):
    if not config.tracing.enabled or not http_client_instrumentor:
        return

    if not hasattr(client, "_is_instrumented_by_opentelemetry"):
        logger.warning("OpenTelemetry has not instrumented this client! Skipping un-instrumentation...")
        return

    http_client_instrumentor.uninstrument_client(client)
