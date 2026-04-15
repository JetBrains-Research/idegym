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

    def _run_test(self, test_name):
        """Helper method to run a test with the given name in Docker."""
        run_test_in_docker(
            self.image,
            self.BASE_CMD.format(test_name),
        )

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
        self._run_test("test_empty_command")

    def test_execute_command_with_working_directory_in_docker(self):
        """Test executing a command with a working directory in a Docker container."""
        self._run_test("test_execute_command_with_working_directory")

    def test_exit_command_in_docker(self):
        """Test executing an exit command in a Docker container."""
        self._run_test("test_exit_command")

    def test_command_with_timeout_in_docker(self):
        """Test that a command with a timeout raises the appropriate exception in a Docker container."""
        self._run_test("test_command_with_timeout")
