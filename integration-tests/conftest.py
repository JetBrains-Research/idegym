import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker",
        action="store_true",
        default=False,
        help="Run tests that require being executed in the Docker",
    )


def pytest_configure(config):
    # Register a custom marker
    config.addinivalue_line("markers", "docker: mark test as requiring Docker environment")


def pytest_collection_modifyitems(config, items):
    # If --run-docker is provided, run all Docker tests
    if config.getoption("--run-docker"):
        return

    # Skip all Docker tests if --run-docker is not provided
    skip_docker = pytest.mark.skip(reason="Need --run-docker option to run")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip_docker)
