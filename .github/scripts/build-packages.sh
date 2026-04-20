#!/usr/bin/env bash

set -euo pipefail

exclude="idegym idegym-orchestrator idegym-server"

for workspace in $(uv workspace list); do
  echo "$exclude" | grep -qw "$workspace" && continue
  uv build --package "$workspace"
done
