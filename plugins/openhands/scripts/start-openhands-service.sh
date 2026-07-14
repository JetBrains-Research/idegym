#!/usr/bin/env bash
# Start the loopback OpenHands Tools Service. Supervised by supervisord as the container's project
# user; binds only to 127.0.0.1. Installed to /usr/local/bin without the
# .sh suffix so it survives IdeGYMServer's /usr/local/bin/*.{py,sh} -> bare-command rename pass.
set -euo pipefail
# Run in the dedicated OpenHands virtualenv (isolated from the IdeGYM server environment). The
# image plugin sets IDEGYM_OPENHANDS_PYTHON to that venv's interpreter.
exec "${IDEGYM_OPENHANDS_PYTHON:-python}" -m idegym.plugins.openhands.service.main
