#!/usr/bin/env bash
# Run the OpenHands compatibility tests in a dedicated, isolated virtualenv.
#
# OpenHands cannot be installed into the IdeGYM monorepo environment: openhands-sdk transitively
# pins opentelemetry-api==1.39.1 (via lmnr) while idegym-backend-utils requires >=1.43.0. This is the
# same reason the OpenHands Tools Service runs in its own in-container venv. So the compatibility
# suite (plugins/openhands compat tests) runs here, in an isolated env mirroring the service venv,
# rather than being skipped in the main `pytest` run.
#
# Usage: plugins/openhands/run-compat-tests.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="${OPENHANDS_COMPAT_VENV:-/tmp/idegym-openhands-compat-venv}"

echo ">> creating isolated venv at ${VENV}"
uv venv "${VENV}" --python 3.12 --clear >/dev/null

echo ">> installing the plugin + OpenHands runtime (isolated from idegym-backend-utils)"
VIRTUAL_ENV="${VENV}" uv pip install --python "${VENV}/bin/python" -q \
    -e "${REPO_ROOT}/api" \
    -e "${REPO_ROOT}/common-utils" \
    -e "${REPO_ROOT}/plugins" \
    "openhands-sdk==1.36.0" "openhands-tools==1.36.0" \
    "fastapi" "fastmcp>=3" "mcp" "uvicorn" "httpx" \
    "pytest" "pytest-asyncio"

echo ">> running compatibility tests"
OPENHANDS_SUPPRESS_BANNER=1 "${VENV}/bin/python" -m pytest \
    "${REPO_ROOT}/unit-tests/test_openhands_compat.py" \
    -o addopts="" -o asyncio_mode=auto -p no:randomly -q "$@"
