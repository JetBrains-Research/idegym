from unittest import IsolatedAsyncioTestCase, main, mock
from unittest.mock import AsyncMock, patch

from idegym.api.status import Status
from idegym.rewards.test_checker import TestChecker as Checker


class TestTestChecker(IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_bash_executor = AsyncMock()
        self.test_checker = Checker(self.mock_bash_executor)

    @patch("idegym.rewards.test_checker.JUnitXml.fromfile")
    async def test_check_repository_tests_success(self, mock_junit_fromfile):
        test_script = "./gradlew test"
        mock_find_output = """
        ./reports/TEST-report1.xml
        ./reports/TEST-report2.xml
        """
        self.mock_bash_executor.execute_bash_command.side_effect = [
            ("Test script output", "", 0),  # Test script execution
            (mock_find_output, "", 0),  # Mock find command output
        ]
        # Mock JUnitXml.fromfile results
        mock_xml_1 = mock.MagicMock()
        mock_xml_1.tests = 10
        mock_xml_1.failures = 2
        mock_xml_1.errors = 1
        mock_xml_1.skipped = 1

        mock_xml_2 = mock.MagicMock()
        mock_xml_2.tests = 5
        mock_xml_2.failures = 0
        mock_xml_2.errors = 0
        mock_xml_2.skipped = 0
        mock_junit_fromfile.side_effect = [mock_xml_1, mock_xml_2]

        result = await self.test_checker.check_repository_tests(test_script)

        expected_scores = {
            "total": 15,  # 10 + 5
            "passed": 11,  # (10 - (2 + 1 + 1)) + (5 - (0 + 0 + 0))
            "failed": 3,  # (2 + 1) + 0
            "skipped": 1,  # 1 + 0
        }
        self.assertEqual(result["status"], Status.SUCCESS)
        self.assertDictEqual(result["scores"], expected_scores)

    async def test_check_repository_tests_failure(self):
        test_script = "./gradlew test"
        mock_find_output = """
        ./reports/TEST-report1.xml
        """
        self.mock_bash_executor.execute_bash_command.side_effect = [
            ("Test script error output", "", 1),  # Test script execution fails
            (mock_find_output, "", 0),  # Mock find command output
        ]
        mock_xml = mock.MagicMock()
        mock_xml.tests = 5
        mock_xml.failures = 1
        mock_xml.errors = 0
        mock_xml.skipped = 1

        with patch("idegym.rewards.test_checker.JUnitXml.fromfile", return_value=mock_xml):
            result = await self.test_checker.check_repository_tests(test_script)

        # Assertions
        expected_scores = {
            "total": 5,
            "passed": 3,  # (5 - (1 + 0 + 1))
            "failed": 1,  # 1 failure
            "skipped": 1,  # 1 skipped
        }
        self.assertEqual(result["status"], Status.FAILURE)
        self.assertDictEqual(result["scores"], expected_scores)

    async def test_check_repository_tests_no_reports_found(self):
        self.mock_bash_executor.execute_bash_command.side_effect = [
            ("Test script output", "", 0),
            ("", "", 0),  # No files found
        ]

        result = await self.test_checker.check_repository_tests()

        expected_scores = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
        self.assertEqual(result["status"], Status.SUCCESS)
        self.assertDictEqual(result["scores"], expected_scores)

    async def test_check_repository_tests_malformed_report_file(self):
        mock_find_output = """
        ./reports/TEST-malformed.xml
        """
        self.mock_bash_executor.execute_bash_command.side_effect = [
            ("Test script output", "", 0),
            (mock_find_output, "", 0),
        ]

        with patch("idegym.rewards.test_checker.JUnitXml.fromfile", side_effect=Exception("Malformed XML")):
            result = await self.test_checker.check_repository_tests()

        expected_scores = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
        self.assertEqual(result["status"], Status.SUCCESS)
        self.assertDictEqual(result["scores"], expected_scores)

    async def test_extract_report_files_success(self):
        output = """
        ./reports/TEST-report1.xml
        ./reports/TEST-report2.xml
        """
        result = self.test_checker._extract_report_files(output)

        self.assertEqual(result, ["./reports/TEST-report1.xml", "./reports/TEST-report2.xml"])

    async def test_extract_report_files_empty(self):
        output = ""
        result = self.test_checker._extract_report_files(output)

        self.assertEqual(result, [])


if __name__ == "__main__":
    main()
