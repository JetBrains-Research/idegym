#!/usr/bin/env bash
# Starts PyCharm Community with an MCP endpoint.
#
# Mode is selected by the MCP_STEROID environment variable:
#
#   MCP_STEROID=false (default)
#     Opens IDEGYM_PROJECT_ROOT via the open-project AppStarter, waits for the
#     bundled JetBrains MCP plugin on port 64342 (/stream or /sse), then bridges
#     0.0.0.0:64343 → 127.0.0.1:64342 via socat.
#
#   MCP_STEROID=true
#     Starts PyCharm without a project (welcome screen); mcp-steroid handles project
#     opening at runtime via the steroid_open_project MCP tool. Waits for
#     mcp-steroid on port 6315 (/mcp), then bridges 0.0.0.0:6316 → 127.0.0.1:6315.
#
# PyCharm CE does not support -Djava.awt.headless=true; Xvfb provides a virtual
# X11 display that satisfies AWT without real hardware.
#
# Log locations:
#   /tmp/pycharm.log                    - IDE launcher stdout/stderr
#   ${IDE_SYSTEM_PATH}/log/idea.log     - PyCharm internal log
#
# Common environment variables (all have defaults):
#   MCP_STEROID      – select mcp-steroid mode                     (default: false)
#   PYCHARM_DIR      – PyCharm installation directory               (default: /opt/pycharm)
#   IDE_SYSTEM_PATH  – PyCharm system/cache/log directory          (default: /tmp/ide-system)
#   IDE_CONFIG_PATH  – PyCharm config directory                    (default: /tmp/ide-config)
#   WAIT_SECONDS     – max seconds to wait for MCP endpoint         (default: 300)
#
# Standard-mode variables:
#   IDEGYM_PROJECT_ROOT  – project to open                         (default: /root/work)
#   MCP_PORT             – bundled MCP plugin listen port          (default: 64342)
#   BRIDGE_PORT          – socat bridge port on 0.0.0.0            (default: 64343)
#
# mcp-steroid mode variables:
#   MCP_STEROID_PORT         – mcp-steroid listen port             (default: 6315)
#   MCP_STEROID_BRIDGE_PORT  – socat bridge port on 0.0.0.0        (default: 6316)

set -euo pipefail

MCP_STEROID="${MCP_STEROID:-false}"
PYCHARM_DIR="${PYCHARM_DIR:-/opt/pycharm}"
IDE_SYSTEM_PATH="${IDE_SYSTEM_PATH:-/tmp/ide-system}"
IDE_CONFIG_PATH="${IDE_CONFIG_PATH:-/tmp/ide-config}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
LOG_FILE="/tmp/pycharm.log"
PYCHARM_LOG="${IDE_SYSTEM_PATH}/log/idea.log"

if [ "${MCP_STEROID}" = "true" ]; then
    LISTEN_PORT="${MCP_STEROID_PORT:-6315}"
    BRIDGE_PORT="${MCP_STEROID_BRIDGE_PORT:-6316}"
else
    PROJECT="${IDEGYM_PROJECT_ROOT:-/root/work}"
    LISTEN_PORT="${MCP_PORT:-64342}"
    BRIDGE_PORT="${BRIDGE_PORT:-64343}"
fi

mkdir -p "${IDE_SYSTEM_PATH}" "${IDE_CONFIG_PATH}/options"

if [ "${MCP_STEROID}" = "true" ]; then
    echo "=== PyCharm + mcp-steroid (no project) ==="
    echo "  mcp-steroid  : http://localhost:${LISTEN_PORT}/mcp"
else
    echo "=== PyCharm Community MCP ==="
    echo "  Project     : ${PROJECT}"
    echo "  MCP URL     : http://localhost:${LISTEN_PORT}/sse"
fi
echo "  PyCharm     : ${PYCHARM_DIR}"
echo "  Config      : ${IDE_CONFIG_PATH}"
echo "  Launcher log: ${LOG_FILE}"
echo "  PyCharm log : ${PYCHARM_LOG}"
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

# ── Start PyCharm in background ───────────────────────────────────────────────
OPEN_ARGS=()
if [ "${MCP_STEROID}" = "true" ]; then
    echo ">>> Launching PyCharm (no project; mcp-steroid handles project opening)"
else
    OPEN_ARGS=("open" "${PROJECT}")
    echo ">>> Launching PyCharm with open-project AppStarter (project: ${PROJECT})"
fi

# -Dmcp.steroid.review.mode=NEVER: run mcp-steroid's steroid_execute_code without the manual
# code-review gate. Its default (ALWAYS) opens a review.kts and waits for a human to approve every
# execution; in this automated run there is no reviewer, so each call hangs until
# mcp.steroid.review.timeout (600s). NEVER auto-approves all executions — required for autonomous
# use (harmless when mcp-steroid is absent, e.g. the default MCP_STEROID=false mode).
"${PYCHARM_DIR}/bin/pycharm.sh" \
    -Didea.trust.all.projects=true \
    -Didea.system.path="${IDE_SYSTEM_PATH}" \
    -Didea.config.path="${IDE_CONFIG_PATH}" \
    -Didea.plugins.path="${PYCHARM_DIR}/plugins" \
    -Dide.no.platform.update=true \
    -Dmcp.steroid.review.mode=NEVER \
    -Dide.show.tips.on.startup.default.value=false \
    -Djb.consents.confirmation.enabled=false \
    "${OPEN_ARGS[@]+"${OPEN_ARGS[@]}"}" > "${LOG_FILE}" 2>&1 &
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

# ── Wait for MCP endpoint ─────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Installed as a bare command (/usr/local/bin/check-mcp) inside IdeGYM images;
# fall back to the .sh name when running from the source tree.
if [ -f "${SCRIPT_DIR}/check-mcp" ]; then
    source "${SCRIPT_DIR}/check-mcp"
else
    source "${SCRIPT_DIR}/check-mcp.sh"
fi

echo "Waiting for MCP endpoint (timeout: ${WAIT_SECONDS}s)..."
for i in $(seq 1 "${WAIT_SECONDS}"); do
    if [ "${MCP_STEROID}" = "true" ]; then
        if check_mcp_endpoint "http://localhost:${LISTEN_PORT}/mcp"; then
            echo ">>> mcp-steroid ready at /mcp (${i}s)"
            break
        fi
    else
        if check_mcp_endpoint "http://localhost:${LISTEN_PORT}/stream"; then
            echo ">>> MCP server ready at /stream (${i}s)"
            break
        fi
        if check_mcp_endpoint "http://localhost:${LISTEN_PORT}/sse"; then
            echo ">>> MCP server ready at /sse (${i}s)"
            break
        fi
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
        echo "ERROR: MCP endpoint not reachable after ${WAIT_SECONDS}s." >&2
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
socat TCP-LISTEN:${BRIDGE_PORT},fork,reuseaddr TCP:127.0.0.1:${LISTEN_PORT} &
SOCAT_PID=$!
echo ">>> socat bridge: 0.0.0.0:${BRIDGE_PORT} -> 127.0.0.1:${LISTEN_PORT} (PID=${SOCAT_PID})"

# ── Keep the container alive until PyCharm exits ──────────────────────────────
echo "Container running. Waiting for PyCharm to exit..."
wait "${PYCHARM_PID}"
