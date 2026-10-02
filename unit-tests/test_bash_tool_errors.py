import json
import subprocess
import sys
from pathlib import Path

import pytest
from idegym.api.exceptions import IdeGYMException
from idegym.backend.utils.bash_executor import (
    BashExecutorRequestError,
    BashExecutorUnknownUserError,
    BashExecutorUserSwitchError,
    BashExecutorWorkingDirectoryError,
)

# Importing `server.main` wires the server's dependency-injection container into its routers
# for the rest of the process, which breaks tests that call those routers directly (test_fs).
# The server app is therefore exercised in a child interpreter.
_REQUEST_BAD_CONTEXT = """
import json, sys
from fastapi.testclient import TestClient
from idegym.backend.utils.bash_executor import BashExecutor
from idegym.tools.file_manager import FileManager
from idegym.tools.router import _get_tool_service
from idegym.tools.tool_service import ToolService
from server.main import app

service = ToolService(bash_executor=BashExecutor(), file_manager=FileManager())
app.dependency_overrides[_get_tool_service] = lambda: service
results = []
with TestClient(app, raise_server_exceptions=False) as client:
    for fields in json.loads(sys.argv[1]):
        response = client.post("/api/tools/bash", json={"command": "true", **fields})
        results.append([response.status_code, response.json()])
print(json.dumps(results))
"""


@pytest.mark.parametrize(
    "error",
    [BashExecutorUnknownUserError, BashExecutorWorkingDirectoryError, BashExecutorUserSwitchError],
)
def test_caller_input_errors_are_idegym_bad_requests(error) -> None:
    assert issubclass(error, BashExecutorRequestError)
    assert issubclass(error, IdeGYMException)


def test_a_bad_cwd_or_user_is_a_400_with_the_standard_error_body() -> None:
    """These used to be a one-off `{detail}` 400 in one router, and a 500 from every other caller."""
    cases = [
        ({"cwd": "/does/not/exist"}, "Working directory does not exist"),
        ({"user": "no-such-user"}, "No such user"),
    ]
    child = subprocess.run(
        [sys.executable, "-c", _REQUEST_BAD_CONTEXT, json.dumps([fields for fields, _ in cases])],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
    )
    results = json.loads(child.stdout.strip().splitlines()[-1])

    for (status_code, body), (_fields, message) in zip(results, cases, strict=True):
        assert status_code == 400
        assert message in body["message"]
        assert {"timestamp", "message", "traceback"} <= body.keys()
