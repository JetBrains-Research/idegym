#!/usr/bin/env bash

set -euo pipefail

# Only public-facing libraries are published to PyPI: idegym-api, idegym-common-utils,
# idegym-client, idegym-image-builder, idegym-plugins. Everything else is internal and
# shipped exclusively inside our Docker images, so it is excluded here.
exclude="idegym idegym-orchestrator idegym-server idegym-watcher idegym-backend-utils idegym-rewards idegym-tools"

for workspace in $(uv workspace list); do
  echo "$exclude" | grep -qw "$workspace" && continue
  uv build --package "$workspace"
done
