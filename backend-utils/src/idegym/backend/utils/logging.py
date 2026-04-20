from logging import DEBUG, StreamHandler
from logging import getLevelNamesMapping as get_level_names_mapping
from logging import getLogger as create_or_get_logger
from logging.handlers import RotatingFileHandler
from sys import stdout

from idegym.api.config import LoggingConfig
from idegym.api.type import LogLevel as Level
from idegym.backend.utils.processor import add_otel_trace_context as OTELTraceContext
from idegym.backend.utils.processor import add_process_id as ProcessID
from idegym.backend.utils.renderer import ConsoleRenderer
from structlog import configure as configure_global_logging
from structlog import get_logger as get_named_logger
from structlog.contextvars import merge_contextvars as MergeContextVars
from structlog.processors import JSONRenderer, TimeStamper
from structlog.processors import dict_tracebacks as DictTracebacks
from structlog.stdlib import BoundLogger, LoggerFactory, ProcessorFormatter
from structlog.stdlib import add_log_level as LogLevel
from structlog.stdlib import add_logger_name as LoggerName
from structlog.stdlib import filter_by_level as FilterByLevel
from structlog.types import Processor

DefaultProcessors: list[Processor] = [
    MergeContextVars,
    LoggerName,
    LogLevel,
    TimeStamper(fmt="iso"),
    ProcessID,
    OTELTraceContext,
]

ConsoleProcessors: list[Processor] = [
    *DefaultProcessors,
]

JSONProcessors: list[Processor] = [
    *DefaultProcessors,
    DictTracebacks,
]


def _create_formatter(renderer: Processor, processors: list[Processor]) -> ProcessorFormatter:
    return ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=processors,
    )


def configure_logging(config: LoggingConfig = LoggingConfig()):
    renderer = JSONRenderer() if config.json_format else ConsoleRenderer()
    processors = JSONProcessors if config.json_format else ConsoleProcessors

    configure_global_logging(
        processors=processors + [FilterByLevel, ProcessorFormatter.wrap_for_formatter],
        logger_factory=LoggerFactory(),
        wrapper_class=BoundLogger,
        cache_logger_on_first_use=True,
    )

    root_logger = create_or_get_logger()
    root_logger.setLevel(config.level)

    console_handler = StreamHandler(stdout)
    console_handler.setFormatter(_create_formatter(renderer=renderer, processors=processors))
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        config.file_path,
        maxBytes=config.max_file_size.bytes,
        backupCount=config.max_file_count,
    )
    file_handler.setFormatter(_create_formatter(renderer=renderer, processors=processors))
    root_logger.addHandler(file_handler)
    get_named_logger(__name__).info(f"Logging to file: {config.file_path}")


def configure_sqlalchemy_logging(config: LoggingConfig = LoggingConfig()):
    level_names_mapping: dict[str, Level] = get_level_names_mapping()
    if level_names_mapping[config.level] == DEBUG:
        create_or_get_logger("sqlalchemy.engine").setLevel(config.level)
        create_or_get_logger("sqlalchemy.pool").setLevel(config.level)


def create_uvicorn_logging_config(config: LoggingConfig = LoggingConfig()):
    renderer = JSONRenderer() if config.json_format else ConsoleRenderer()
    processors = JSONProcessors if config.json_format else ConsoleProcessors
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": ProcessorFormatter,
                "processor": renderer,
                "foreign_pre_chain": processors,
            },
            "file_formatter": {
                "()": ProcessorFormatter,
                "processor": renderer,
                "foreign_pre_chain": processors,
            },
        },
        "handlers": {
            "console": {
                "class": StreamHandler,
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "class": RotatingFileHandler,
                "formatter": "default",
                "filename": config.file_path,
                "maxBytes": config.max_file_size.bytes,
                "backupCount": config.max_file_count,
            },
        },
        "root": {
            "level": config.level,
            "handlers": ["console", "file"],
        },
    }
