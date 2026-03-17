#!/usr/bin/env bash

set -eu;

__DEFAULT_IDEGYM_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IDEGYM_PATH="${IDEGYM_PATH:-$__DEFAULT_IDEGYM_PATH}"

# In the event that signals are received by the shell process, it will not forward said signals to the child.
# This means that the shell process will stop, but the child will continue to run.
# Since the command that creates the child process is the last one in the shell script,
# then the aforementioned problem can be easily solved using `exec`
# (excerpt from: https://veithen.io/2014/11/16/sigterm-propagation.html).
exec "$IDEGYM_PATH/.venv/bin/python" "$IDEGYM_PATH/entrypoint.py" "$@";
