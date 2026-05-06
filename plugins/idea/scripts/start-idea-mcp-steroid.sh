#!/usr/bin/env bash
# Starts IntelliJ IDEA headless with mcp-steroid without opening a specific project.
# mcp-steroid (https://github.com/jonnyzzz/mcp-steroid) must be installed in
# ${IDE_DIR}/plugins/ at image build time via Dockerfile.mcp_steroid.j2.
#
# After startup, agents can open any project via the mcp-steroid "open-project"
# MCP tool at http://localhost:6315/mcp.
#
# Unlike PyCharm, IDEA fully supports -Djava.awt.headless=true, so no display
# server (Xvfb) is required.
#
# Log locations:
#   /tmp/idea.log                       - IDE launcher stdout/stderr
#   ${IDE_SYSTEM_PATH}/log/idea.log     - IDEA internal log (idea.log)
#
# Environment variables (all have defaults):
#   IDE_DIR                  – IDEA installation directory           (default: /opt/idea)
#   IDE_SYSTEM_PATH          – IDEA system/cache/log directory      (default: /tmp/ide-system)
#   IDE_CONFIG_PATH          – IDEA config directory                (default: /tmp/ide-config)
#   MCP_STEROID_PORT         – mcp-steroid listen port              (default: 6315)
#   MCP_STEROID_BRIDGE_PORT  – socat bridge port on 0.0.0.0         (default: 6316)
#   WAIT_SECONDS             – max seconds to wait for mcp-steroid  (default: 120)

set -euo pipefail

IDE_DIR="${IDE_DIR:-/opt/idea}"
IDE_SYSTEM_PATH="${IDE_SYSTEM_PATH:-/tmp/ide-system}"
IDE_CONFIG_PATH="${IDE_CONFIG_PATH:-/tmp/ide-config}"
MCP_STEROID_PORT="${MCP_STEROID_PORT:-6315}"
MCP_STEROID_BRIDGE_PORT="${MCP_STEROID_BRIDGE_PORT:-6316}"
WAIT_SECONDS="${WAIT_SECONDS:-120}"
MCP_URL="http://localhost:${MCP_STEROID_PORT}/mcp"
LOG_FILE="/tmp/idea.log"
IDEA_LOG="${IDE_SYSTEM_PATH}/log/idea.log"

mkdir -p "${IDE_SYSTEM_PATH}" "${IDE_CONFIG_PATH}/options"

echo "=== IDEA + mcp-steroid (no project) ==="
echo "  mcp-steroid  : ${MCP_URL}"
echo "  IDEA         : ${IDE_DIR}"
echo "  Config       : ${IDE_CONFIG_PATH}"
echo "  Launcher log : ${LOG_FILE}"
echo "  IDEA log     : ${IDEA_LOG}"
echo ""

# ── Start IDEA headless without a project ─────────────────────────────────────
# No "open <project>" argument — IDEA opens to the welcome screen headlessly.
# mcp-steroid loads as a plugin and starts its MCP server on port 6315.
export JAVA_TOOL_OPTIONS="-Djava.awt.headless=true"

echo ">>> Launching IDEA headless (no project; mcp-steroid handles project opening)"
"${IDE_DIR}/bin/idea.sh" \
    -Djava.awt.headless=true \
    -Didea.platform.prefix=Idea \
    -Didea.trust.all.projects=true \
    -Dide.no.platform.update=true \
    -Didea.system.path="${IDE_SYSTEM_PATH}" \
    -Didea.config.path="${IDE_CONFIG_PATH}" \
    -Didea.plugins.path="${IDE_DIR}/plugins" \
    > "${LOG_FILE}" 2>&1 &
IDEA_PID=$!
echo "IDEA started (PID=${IDEA_PID})"

# ── Graceful shutdown on SIGTERM / SIGINT ─────────────────────────────────────
SOCAT_PID=""
cleanup() {
    echo ""
    echo "Shutting down IDEA (PID=${IDEA_PID})..."
    kill "${SOCAT_PID}" 2>/dev/null || true
    kill "${IDEA_PID}" 2>/dev/null || true
    for _ in $(seq 1 10); do
        kill -0 "${IDEA_PID}" 2>/dev/null || { echo "IDEA exited."; return; }
        sleep 1
    done
    kill -9 "${IDEA_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Wait for mcp-steroid MCP endpoint ─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/check-mcp.sh"

echo "Waiting for mcp-steroid at ${MCP_URL} (timeout: ${WAIT_SECONDS}s)..."
for i in $(seq 1 "${WAIT_SECONDS}"); do
    if check_mcp_endpoint "${MCP_URL}"; then
        echo ">>> mcp-steroid ready at ${MCP_URL} (${i}s)"
        break
    fi

    if ! kill -0 "${IDEA_PID}" 2>/dev/null; then
        echo "ERROR: IDEA process (PID=${IDEA_PID}) exited unexpectedly." >&2
        echo "--- last 30 lines of ${LOG_FILE} ---" >&2
        tail -30 "${LOG_FILE}" >&2
        if [ -f "${IDEA_LOG}" ]; then
            echo "--- last 50 lines of ${IDEA_LOG} ---" >&2
            tail -50 "${IDEA_LOG}" >&2
        fi
        exit 1
    fi

    if [[ "${i}" -eq "${WAIT_SECONDS}" ]]; then
        echo "ERROR: mcp-steroid not reachable after ${WAIT_SECONDS}s." >&2
        echo "--- last 30 lines of ${LOG_FILE} ---" >&2
        tail -30 "${LOG_FILE}" >&2
        if [ -f "${IDEA_LOG}" ]; then
            echo "--- last 50 lines of ${IDEA_LOG} ---" >&2
            tail -50 "${IDEA_LOG}" >&2
        fi
        exit 1
    fi

    if (( i % 30 == 0 )); then
        if [ -f "${IDEA_LOG}" ]; then
            echo "--- idea.log tail at ${i}s ---"
            tail -10 "${IDEA_LOG}"
        fi
    fi

    sleep 1
done

# ── Start socat bridge ────────────────────────────────────────────────────────
socat TCP-LISTEN:${MCP_STEROID_BRIDGE_PORT},fork,reuseaddr TCP:127.0.0.1:${MCP_STEROID_PORT} &
SOCAT_PID=$!
echo ">>> socat bridge: 0.0.0.0:${MCP_STEROID_BRIDGE_PORT} -> 127.0.0.1:${MCP_STEROID_PORT} (PID=${SOCAT_PID})"
echo "Host access: http://localhost:${MCP_STEROID_BRIDGE_PORT}/mcp"

# ── Keep the container alive until IDEA exits ─────────────────────────────────
echo "Container running. Waiting for IDEA to exit..."
wait "${IDEA_PID}"
