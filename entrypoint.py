from datetime import datetime, timezone
from glob import glob
from logging import ERROR, INFO, LogRecord, StreamHandler
from logging import Formatter as BaseFormatter
from logging import basicConfig as configure_logging
from logging import getLogger as get_logger
from os import X_OK, access, execlp
from pathlib import Path
from subprocess import CalledProcessError, run
from sys import argv, stdout
from typing import Optional


class Formatter(BaseFormatter):
    def format(self, record: LogRecord) -> str:
        original = record.levelname
        record.levelname = original.lower()
        result = super().format(record)
        record.levelname = original
        return result

    @staticmethod
    def _converter(*_):
        return datetime.now(timezone.utc).timetuple()

    converter = _converter
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%06dZ"


configure_logging(level="INFO", handlers=[StreamHandler(stdout)])

formatter = Formatter(fmt="%(asctime)s [%(levelname)s] [%(module)s] %(message)s")

for handler in get_logger().handlers:
    handler.setFormatter(formatter)

logger = get_logger(__name__)

entrypoint = Path("/docker-entrypoint.d")


def loglines(output: Optional[str], level: int):
    lines = output.splitlines() if output else []
    for raw in lines:
        if line := raw.strip():
            logger.log(level, line)


def execute(script: str):
    if access(script, X_OK):
        logger.info("Executing: %s", script)
        process = run(
            args=[script],
            check=True,
            capture_output=True,
            text=True,
        )
        loglines(output=process.stdout, level=INFO)
    else:
        logger.info("Executing via shell: %s", script)
        process = run(
            args=["/bin/bash", script],
            check=True,
            capture_output=True,
            text=True,
        )
        loglines(output=process.stdout, level=INFO)


def main():
    logger.info("Scanning entrypoint directory for user scripts: %s", entrypoint)
    scripts: list[str] = [script for script in glob(f"{entrypoint}/*.sh")]
    if not scripts:
        logger.info("No user scripts found!")
    for script in scripts:
        try:
            execute(script)
        except CalledProcessError as ex:
            logger.error("Failed to execute user script: %s", script)
            loglines(output=ex.stderr, level=ERROR)
            exit(1)

    logger.info("Initialization complete! Starting up...")


if __name__ == "__main__":
    if entrypoint.exists() and entrypoint.is_dir():
        main()
    if len(argv) > 1:
        _, cmd, *args = argv
        # https://stackoverflow.com/a/6743663/17173324
        execlp(cmd, cmd, *args)
