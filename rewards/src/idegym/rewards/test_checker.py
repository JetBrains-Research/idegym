from typing import Dict

from idegym.api.status import Status
from idegym.backend.utils.bash_executor import BashExecutor
from idegym.utils.logging import get_logger
from junitparser import JUnitXml

logger = get_logger(__name__)


class TestChecker:
    def __init__(self, bash_executor: BashExecutor):
        self.bash_executor = bash_executor

    async def check_repository_tests(
        self,
        test_script: str = "python -m pytest --junitxml=test-results.xml",
        timeout: float = 600.0,
        graceful_termination_timeout: float = 2.0,
    ) -> Dict:
        _, _, exit_code = await self.bash_executor.execute_bash_command(
            test_script, timeout, graceful_termination_timeout
        )

        find_command = "find . -type f -iname 'test-*.xml' -exec realpath {} \\;"
        report_files_stdout, _, _ = await self.bash_executor.execute_bash_command(
            find_command, timeout, graceful_termination_timeout
        )
        report_files = self._extract_report_files(report_files_stdout)

        total, passed, failed, skipped = 0, 0, 0, 0
        for report_file in report_files:
            try:
                parsed_report = JUnitXml.fromfile(report_file)
                total += parsed_report.tests
                passed += parsed_report.tests - (parsed_report.failures + parsed_report.errors + parsed_report.skipped)
                failed += parsed_report.failures + parsed_report.errors
                skipped += parsed_report.skipped
            except Exception:
                logger.exception("An error occurred while processing the report file")
                continue

        return {
            "status": Status.FAILURE if exit_code != 0 else Status.SUCCESS,
            "scores": {
                "total": total,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
            },
        }

    def _extract_report_files(self, output):
        if not output:
            return []

        return [line.strip() for line in output.strip().split("\n")]
