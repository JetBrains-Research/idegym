#!/usr/bin/env bash
# Starts IntelliJ IDEA Community in headless mode with MCP server enabled,
# waits until the MCP endpoint is reachable, then blocks (keeping the container
# alive).
#
# Unlike PyCharm CE, IDEA Community fully supports -Djava.awt.headless=true, so
# no display server (Xvfb) is required.
#
# Log locations:
#   /tmp/idea.log                       - IDE launcher stdout/stderr
#   ${IDE_SYSTEM_PATH}/log/idea.log     - IDEA internal log (idea.log)
#
# Environment variables used at runtime (all have defaults):
#   IDEGYM_PROJECT_ROOT  – project to open                      (default: /root/work)
#   IDE_DIR              – IDEA installation directory           (default: /opt/idea)
#   IDE_SYSTEM_PATH      – IDEA system/cache/log directory      (default: /tmp/ide-system)
#   IDE_CONFIG_PATH      – IDEA config directory                (default: /tmp/ide-config)
#   MCP_PORT             – port the MCP endpoint listens on      (/sse or /stream, default: 64342)
#   BRIDGE_PORT          – port socat exposes on 0.0.0.0         (default: 64343)
#   WAIT_SECONDS         – max seconds to wait for MCP endpoint  (default: 120)

set -euo pipefail

PROJECT="${IDEGYM_PROJECT_ROOT:-/root/work}"
IDE_DIR="${IDE_DIR:-/opt/idea}"
IDE_SYSTEM_PATH="${IDE_SYSTEM_PATH:-/tmp/ide-system}"
IDE_CONFIG_PATH="${IDE_CONFIG_PATH:-/tmp/ide-config}"
MCP_PORT="${MCP_PORT:-64342}"
BRIDGE_PORT="${BRIDGE_PORT:-64343}"
WAIT_SECONDS="${WAIT_SECONDS:-120}"
MCP_URL="http://localhost:${MCP_PORT}/sse"
LOG_FILE="/tmp/idea.log"
IDEA_LOG="${IDE_SYSTEM_PATH}/log/idea.log"

mkdir -p "${IDE_SYSTEM_PATH}" "${IDE_CONFIG_PATH}/options"

echo "=== IntelliJ IDEA Community MCP ==="
echo "  Project     : ${PROJECT}"
echo "  IDEA        : ${IDE_DIR}"
echo "  MCP URL     : ${MCP_URL}"
echo "  Config      : ${IDE_CONFIG_PATH}"
echo "  Launcher log: ${LOG_FILE}"
echo "  IDEA log    : ${IDEA_LOG}"
echo ""

# ── Start IDEA in background ──────────────────────────────────────────────────
# JAVA_TOOL_OPTIONS is read by the JVM before any application startup code,
# making it the most reliable way to set java.awt.headless=true for
# ApplicationStarter commands (where the headless check runs very early).
export JAVA_TOOL_OPTIONS="-Djava.awt.headless=true"

echo ">>> Launching IDEA with open-project AppStarter (project: ${PROJECT})"
"${IDE_DIR}/bin/idea.sh" \
    -Djava.awt.headless=true \
    -Didea.platform.prefix=Idea \
    -Didea.trust.all.projects=true \
    -Dide.no.platform.update=true \
    -Didea.system.path="${IDE_SYSTEM_PATH}" \
    -Didea.config.path="${IDE_CONFIG_PATH}" \
    -Didea.plugins.path="${IDE_DIR}/plugins" \
    open "${PROJECT}" > "${LOG_FILE}" 2>&1 &
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

# ── Wait for the MCP endpoint (stream or SSE) ────────────────────────────────
# Source the check-mcp.sh script to get the check_mcp_endpoint function
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/check-mcp.sh"

echo "Waiting for MCP endpoint (checking /stream and /sse, timeout: ${WAIT_SECONDS}s)..."
MCP_READY=false
for i in $(seq 1 "${WAIT_SECONDS}"); do
    # Try /stream endpoint first (newer versions)
    if check_mcp_endpoint "http://localhost:${MCP_PORT}/stream"; then
        echo ">>> MCP server ready at /stream (${i}s)"
        MCP_URL="http://localhost:${MCP_PORT}/stream"
        MCP_READY=true
        break
    fi

    # Try /sse endpoint (legacy)
    if check_mcp_endpoint "http://localhost:${MCP_PORT}/sse"; then
        echo ">>> MCP server ready at /sse (${i}s)"
        MCP_URL="http://localhost:${MCP_PORT}/sse"
        MCP_READY=true
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
        echo "ERROR: MCP endpoint not reachable after ${WAIT_SECONDS}s." >&2
        echo "--- last 30 lines of ${LOG_FILE} ---" >&2
        tail -30 "${LOG_FILE}" >&2
        if [ -f "${IDEA_LOG}" ]; then
            echo "--- last 50 lines of ${IDEA_LOG} ---" >&2
            tail -50 "${IDEA_LOG}" >&2
        fi
        exit 1
    fi

    # Print idea.log tail every 30 s so it's visible in container logs
    if (( i % 30 == 0 )); then
        if [ -f "${IDEA_LOG}" ]; then
            echo "--- idea.log tail at ${i}s ---"
            tail -10 "${IDEA_LOG}"
        fi
    fi

    sleep 1
done

# ── Start socat bridge ────────────────────────────────────────────────────────
socat TCP-LISTEN:${BRIDGE_PORT},fork,reuseaddr TCP:127.0.0.1:${MCP_PORT} &
SOCAT_PID=$!
echo ">>> socat bridge: 0.0.0.0:${BRIDGE_PORT} -> 127.0.0.1:${MCP_PORT} (PID=${SOCAT_PID})"

# ── Keep the container alive until IDEA exits ─────────────────────────────────
echo "Container running. Waiting for IDEA to exit..."
wait "${IDEA_PID}"
