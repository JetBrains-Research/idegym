#!/usr/bin/env bash
# Starts PyCharm with mcp-steroid without opening a specific project.
# mcp-steroid (https://github.com/jonnyzzz/mcp-steroid) must be installed in
# ${PYCHARM_DIR}/plugins/ at image build time via Dockerfile.mcp_steroid.j2.
#
# After startup, agents can open any project via the mcp-steroid "open-project"
# MCP tool at http://localhost:6315/mcp.
#
# Log locations:
#   /tmp/pycharm.log                    - IDE launcher stdout/stderr
#   ${IDE_SYSTEM_PATH}/log/idea.log     - PyCharm internal log (idea.log)
#
# Environment variables (all have defaults):
#   PYCHARM_DIR              – PyCharm installation directory        (default: /opt/pycharm)
#   IDE_SYSTEM_PATH          – PyCharm system/cache/log directory   (default: /tmp/ide-system)
#   IDE_CONFIG_PATH          – PyCharm config directory             (default: /tmp/ide-config)
#   MCP_STEROID_PORT         – mcp-steroid listen port              (default: 6315)
#   MCP_STEROID_BRIDGE_PORT  – socat bridge port on 0.0.0.0         (default: 6316)
#   WAIT_SECONDS             – max seconds to wait for mcp-steroid  (default: 300)

set -euo pipefail

PYCHARM_DIR="${PYCHARM_DIR:-/opt/pycharm}"
IDE_SYSTEM_PATH="${IDE_SYSTEM_PATH:-/tmp/ide-system}"
IDE_CONFIG_PATH="${IDE_CONFIG_PATH:-/tmp/ide-config}"
MCP_STEROID_PORT="${MCP_STEROID_PORT:-6315}"
MCP_STEROID_BRIDGE_PORT="${MCP_STEROID_BRIDGE_PORT:-6316}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
MCP_URL="http://localhost:${MCP_STEROID_PORT}/mcp"
LOG_FILE="/tmp/pycharm.log"
PYCHARM_LOG="${IDE_SYSTEM_PATH}/log/idea.log"

mkdir -p "${IDE_SYSTEM_PATH}" "${IDE_CONFIG_PATH}/options"

echo "=== PyCharm + mcp-steroid (no project) ==="
echo "  mcp-steroid  : ${MCP_URL}"
echo "  PyCharm      : ${PYCHARM_DIR}"
echo "  Config       : ${IDE_CONFIG_PATH}"
echo "  Launcher log : ${LOG_FILE}"
echo "  PyCharm log  : ${PYCHARM_LOG}"
echo ""

# ── Start Xvfb (virtual display) ──────────────────────────────────────────────
# PyCharm CE does not support -Djava.awt.headless=true; Xvfb provides a virtual
# X11 display that satisfies AWT without real hardware.
pkill -x Xvfb 2>/dev/null || true
sleep 0.2
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
XVFB_PID=$!
echo "Xvfb started (PID=${XVFB_PID}, DISPLAY=${DISPLAY})"
sleep 1

# ── Start PyCharm without a project ───────────────────────────────────────────
# No "open <project>" argument — PyCharm opens to the welcome screen.
# mcp-steroid loads as a plugin and starts its MCP server on port 6315.
echo ">>> Launching PyCharm (no project; mcp-steroid handles project opening)"
"${PYCHARM_DIR}/bin/pycharm.sh" \
    -Didea.trust.all.projects=true \
    -Didea.system.path="${IDE_SYSTEM_PATH}" \
    -Didea.config.path="${IDE_CONFIG_PATH}" \
    -Didea.plugins.path="${PYCHARM_DIR}/plugins" \
    -Dide.no.platform.update=true \
    -Dide.show.tips.on.startup.default.value=false \
    -Djb.consents.confirmation.enabled=false \
    > "${LOG_FILE}" 2>&1 &
PYCHARM_PID=$!
echo "PyCharm started (PID=${PYCHARM_PID})"

# ── Graceful shutdown on SIGTERM / SIGINT ─────────────────────────────────────
SOCAT_PID=""
cleanup() {
    echo ""
    echo "Shutting down PyCharm (PID=${PYCHARM_PID})..."
    kill "${SOCAT_PID}" 2>/dev/null || true
    kill "${PYCHARM_PID}" 2>/dev/null || true
    for _ in $(seq 1 10); do
        kill -0 "${PYCHARM_PID}" 2>/dev/null || { echo "PyCharm exited."; break; }
        sleep 1
    done
    kill -9 "${PYCHARM_PID}" 2>/dev/null || true
    kill "${XVFB_PID}" 2>/dev/null || true
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

    if ! kill -0 "${PYCHARM_PID}" 2>/dev/null; then
        echo "ERROR: PyCharm process (PID=${PYCHARM_PID}) exited unexpectedly." >&2
        echo "--- last 30 lines of ${LOG_FILE} ---" >&2
        tail -30 "${LOG_FILE}" >&2
        if [ -f "${PYCHARM_LOG}" ]; then
            echo "--- last 50 lines of ${PYCHARM_LOG} ---" >&2
            tail -50 "${PYCHARM_LOG}" >&2
        fi
        exit 1
    fi

    if [[ "${i}" -eq "${WAIT_SECONDS}" ]]; then
        echo "ERROR: mcp-steroid not reachable after ${WAIT_SECONDS}s." >&2
        echo "--- last 30 lines of ${LOG_FILE} ---" >&2
        tail -30 "${LOG_FILE}" >&2
        if [ -f "${PYCHARM_LOG}" ]; then
            echo "--- last 50 lines of ${PYCHARM_LOG} ---" >&2
            tail -50 "${PYCHARM_LOG}" >&2
        fi
        exit 1
    fi

    if (( i % 30 == 0 )); then
        if [ -f "${PYCHARM_LOG}" ]; then
            echo "--- pycharm log tail at ${i}s ---"
            tail -10 "${PYCHARM_LOG}"
        fi
    fi

    sleep 1
done

# ── Start socat bridge ────────────────────────────────────────────────────────
socat TCP-LISTEN:${MCP_STEROID_BRIDGE_PORT},fork,reuseaddr TCP:127.0.0.1:${MCP_STEROID_PORT} &
SOCAT_PID=$!
echo ">>> socat bridge: 0.0.0.0:${MCP_STEROID_BRIDGE_PORT} -> 127.0.0.1:${MCP_STEROID_PORT} (PID=${SOCAT_PID})"
echo "Host access: http://localhost:${MCP_STEROID_BRIDGE_PORT}/mcp"

# ── Keep the container alive until PyCharm exits ──────────────────────────────
echo "Container running. Waiting for PyCharm to exit..."
wait "${PYCHARM_PID}"
