"""Which stream ``configure_logging`` writes its records to.

Services log to stdout, but ``idegym.orchestrator.db_cli`` prints values a caller captures
there, so it sends its log records to stderr instead. That separation is what makes
``REV=$(... db_cli schema current)`` return a revision rather than a revision buried in log
lines, so it is worth a test of its own.
"""

import logging
import sys
from collections.abc import Iterator
from io import StringIO

import pytest
from idegym.backend.utils.logging import configure_logging
from idegym.utils.logging import get_logger


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """Put the root logger back: ``configure_logging`` replaces its handlers globally."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)


def console_streams() -> list[object]:
    """Streams of the non-file handlers — what a terminal or ``kubectl logs`` sees."""
    return [
        handler.stream
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler)
    ]


def test_logging_goes_to_stdout_by_default():
    configure_logging()

    assert sys.stdout in console_streams()


def test_a_command_line_entry_point_can_send_its_logs_elsewhere():
    stream = StringIO()

    configure_logging(stream=stream)
    get_logger(__name__).info("migrating")

    assert console_streams() == [stream]
    assert "migrating" in stream.getvalue()
