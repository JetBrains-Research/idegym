from os import getpid

from opentelemetry import trace
from structlog.typing import EventDict, WrappedLogger


def add_otel_trace_context(
    _logger: WrappedLogger,
    _name: str,
    event_dict: EventDict,
) -> EventDict:
    span = trace.get_current_span()
    if not span.is_recording():
        return event_dict

    context = span.get_span_context()
    if not context.is_valid:
        return event_dict

    event_dict["span_id"] = format(context.span_id, "016x")
    event_dict["trace_id"] = format(context.trace_id, "032x")
    event_dict["trace_sampled"] = context.trace_flags.sampled

    return event_dict


def add_process_id(
    _logger: WrappedLogger,
    _name: str,
    event_dict: EventDict,
) -> EventDict:
    event_dict["pid"] = getpid()
    return event_dict
