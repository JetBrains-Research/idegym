"""Which stream ``configure_logging`` writes its records to.

Services log to stdout, but ``idegym.orchestrator.db_cli`` prints captured values there, so
it logs to stderr instead. That separation is what makes ``REV=$(... schema current)`` a
revision rather than a revision buried in log lines.
"""

import logging
import sys
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from secrets import token_hex

import pytest
from idegym.api.config import DatabaseConfig, LoggingConfig, SQLAlchemyConfig
from idegym.backend.utils.logging import configure_logging
from idegym.orchestrator.database.database import connect_db_engine
from idegym.orchestrator.migration_manager import MigrationManager
from idegym.utils.logging import get_logger
from pydantic import SecretStr
from pytest import param
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from structlog.testing import capture_logs


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


# Generated, not a literal: the console traceback quotes the source lines around each frame, so a
# literal secret would show up in the output even when no local is rendered.
PASSWORD = token_hex(16)


def fail_holding(password: str) -> None:
    """Raise while a frame holds the credential, as ``_task_start_server`` holds the ``Config``.

    The local is a plain ``str`` so only ``show_locals=False`` keeps it out of the output; a
    ``SecretStr`` would be masked by its own repr and hide a regression.
    """
    raise RuntimeError("cannot reach the database")


@pytest.mark.parametrize("json_format", [param(True, id="json"), param(False, id="console")])
def test_a_traceback_does_not_print_the_secrets_in_scope(tmp_path: Path, json_format: bool):
    stream = StringIO()
    configure_logging(LoggingConfig(json_format=json_format, file_path=str(tmp_path / "idegym.log")), stream=stream)

    try:
        fail_holding(PASSWORD)
    except RuntimeError:
        get_logger(__name__).exception("Error creating IdeGYM server")

    output = stream.getvalue()
    assert "fail_holding" in output, "the traceback itself must still be logged"
    assert PASSWORD not in output


def test_the_database_password_is_masked_everywhere_but_the_url():
    database = DatabaseConfig(password=SecretStr(PASSWORD))

    assert PASSWORD not in repr(database)
    assert PASSWORD not in str(database)
    assert PASSWORD in database.url


async def test_connecting_to_the_database_does_not_log_the_password():
    database = DatabaseConfig(password=SecretStr(PASSWORD))

    with capture_logs() as records:
        engine = connect_db_engine(database.url, SQLAlchemyConfig())
    await engine.dispose()

    [connected] = [record for record in records if record["event"] == "Connected to database"]
    assert database.host in connected["url"]
    assert PASSWORD not in str(records)


# Every character that changes the meaning of a URL, plus the one ConfigParser interpolates.
AWKWARD_PASSWORD = "p@ss:w/rd%zz?#"


def test_a_password_with_url_syntax_still_reaches_the_right_host():
    url = make_url(DatabaseConfig(host="db.internal", password=SecretStr(AWKWARD_PASSWORD)).url)

    assert url.host == "db.internal"
    assert url.password == AWKWARD_PASSWORD


def test_alembic_receives_the_url_unchanged():
    url = DatabaseConfig(password=SecretStr(AWKWARD_PASSWORD)).url
    manager = MigrationManager(engine=create_async_engine(url), db_url=url)

    assert manager._alembic_config().get_main_option("sqlalchemy.url") == url
