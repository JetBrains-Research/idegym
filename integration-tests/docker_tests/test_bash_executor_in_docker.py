from .test_utils import build_docker_image, run_test_in_docker


class TestBashExecutorInDocker:
    """
    Docker environment for BashExecutor tests.

    These tests build a Docker image and run the tests from test_bash_executor.py
    inside the container.
    """

    BASE_CMD = "pytest integration-tests/docker_tests/test_bash_executor.py::TestBashExecutor::{} -v"

    @classmethod
    def setup_class(cls):
        """Build the Docker image once for all tests."""
        cls.image = build_docker_image()

    def _run_test(self, test_name, user=None):
        """Helper method to run a test with the given name in Docker, optionally as a non-root user."""
        command = self.BASE_CMD.format(test_name)
        if user is not None:
            # /app is root-owned, so a non-root pytest cannot write its cache there.
            command += " -p no:cacheprovider"
        run_test_in_docker(self.image, command, user=user)

    def test_execute_valid_command_in_docker(self):
        """Test executing a valid command in a Docker container."""
        self._run_test("test_execute_valid_command")

    def test_execute_command_with_stderr_in_docker(self):
        """Test executing a command that produces stderr output in a Docker container."""
        self._run_test("test_execute_command_with_stderr")

    def test_execute_command_with_non_zero_exit_code_in_docker(self):
        """Test executing a command that returns a non-zero exit code in a Docker container."""
        self._run_test("test_execute_command_with_non_zero_exit_code")

    def test_empty_command_in_docker(self):
        """Test executing an empty command in a Docker container."""
        self._run_test("test_empty_command_is_a_no_op")

    def test_execute_command_with_working_directory_in_docker(self):
        """Test executing a command with a working directory in a Docker container."""
        self._run_test("test_execute_command_with_working_directory")

    def test_exit_command_in_docker(self):
        """Test executing an exit command in a Docker container."""
        self._run_test("test_exit_command")

    def test_command_with_timeout_in_docker(self):
        """Test that a command with a timeout raises the appropriate exception in a Docker container."""
        self._run_test("test_command_with_timeout")

    def test_a_bashrc_ending_in_a_failing_command_does_not_abort_the_script_in_docker(self):
        self._run_test("test_a_bashrc_ending_in_a_failing_command_does_not_abort_the_script")

    def test_a_callers_path_does_not_affect_finding_bash_in_docker(self):
        self._run_test("test_a_callers_path_does_not_affect_finding_bash")

    def test_bash_reports_errors_at_the_callers_own_line_numbers_in_docker(self):
        self._run_test("test_bash_reports_errors_at_the_callers_own_line_numbers")

    def test_errors_carry_the_bash_c_prefix_not_the_temp_file_name_in_docker(self):
        self._run_test("test_errors_carry_the_bash_c_prefix_not_the_temp_file_name")

    def test_dollar_zero_is_bash_and_there_are_no_positional_parameters_in_docker(self):
        self._run_test("test_dollar_zero_is_bash_and_there_are_no_positional_parameters")

    def test_a_user_switch_as_root_goes_through_runuser_in_docker(self):
        self._run_test("test_a_user_switch_keeps_the_script_private_and_the_environment_intact")

    def test_a_user_switch_as_appuser_goes_through_sudo_in_docker(self):
        self._run_test("test_a_user_switch_keeps_the_script_private_and_the_environment_intact", user="appuser")

    def test_a_timeout_under_sudo_still_returns_in_docker(self):
        self._run_test("test_a_timeout_under_a_user_switch_still_returns", user="appuser")

    def test_a_user_switch_without_root_or_sudo_is_rejected_in_docker(self):
        self._run_test("test_a_user_switch_without_root_or_sudo_is_rejected", user="devuser")

    def test_the_servers_own_user_needs_no_switch_in_docker(self):
        self._run_test("test_the_servers_own_user_needs_no_switch", user="devuser")
