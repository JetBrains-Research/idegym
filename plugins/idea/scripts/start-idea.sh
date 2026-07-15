#!/usr/bin/env bash
# Starts IntelliJ IDEA Community with an MCP endpoint.
#
# Display mode is selected by the IDEA_HEADLESS environment variable (baked in by
# the Idea image plugin):
#
#   IDEA_HEADLESS=true (default)
#     IDEA launches in true headless mode (-Djava.awt.headless=true); no display
#     server is required. Starts faster and uses less memory.
#
#   IDEA_HEADLESS=false
#     Xvfb provides a virtual X11 display on :99 and IDEA launches with a real
#     AWT toolkit — the same way PyCharm always runs. Use for workloads that need
#     a display (Swing UI rendering, plugins that break under headless AWT).
#
# Mode is selected by the MCP_STEROID environment variable:
#
#   MCP_STEROID=false (default)
#     Opens IDEGYM_PROJECT_ROOT via the open-project AppStarter, waits for the
#     bundled JetBrains MCP plugin on port 64342 (/stream or /sse), then bridges
#     0.0.0.0:64343 → 127.0.0.1:64342 via socat.
#
#   MCP_STEROID=true
#     Starts IDEA without a project (welcome screen); mcp-steroid handles project
#     opening at runtime via the steroid_open_project MCP tool. Waits for
#     mcp-steroid on port 6315 (/mcp), then bridges 0.0.0.0:6316 → 127.0.0.1:6315.
#
# IDEA fully supports -Djava.awt.headless=true, so Xvfb is only started when
# IDEA_HEADLESS=false (unlike PyCharm, which always needs it).
#
# Log locations:
#   /tmp/idea.log                       - IDE launcher stdout/stderr
#   ${IDE_SYSTEM_PATH}/log/idea.log     - IDEA internal log
#
# Common environment variables (all have defaults):
#   MCP_STEROID      – select mcp-steroid mode                     (default: false)
#   IDEA_HEADLESS    – run headless (true) or with Xvfb (false)     (default: true)
#   IDE_DIR          – IDEA installation directory                  (default: /opt/idea)
#   IDE_SYSTEM_PATH  – IDEA system/cache/log directory             (default: /tmp/ide-system)
#   IDE_CONFIG_PATH  – IDEA config directory                       (default: /tmp/ide-config)
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
IDEA_HEADLESS="${IDEA_HEADLESS:-true}"
IDE_DIR="${IDE_DIR:-/opt/idea}"
IDE_SYSTEM_PATH="${IDE_SYSTEM_PATH:-/tmp/ide-system}"
IDE_CONFIG_PATH="${IDE_CONFIG_PATH:-/tmp/ide-config}"
# Non-headless (Xvfb) startup + indexing is slower than headless, so allow the same
# generous MCP-endpoint budget PyCharm uses. Headless still breaks out early on success.
WAIT_SECONDS="${WAIT_SECONDS:-300}"
LOG_FILE="/tmp/idea.log"
IDEA_LOG="${IDE_SYSTEM_PATH}/log/idea.log"

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
    echo "=== IntelliJ IDEA + mcp-steroid (no project) ==="
    echo "  mcp-steroid  : http://localhost:${LISTEN_PORT}/mcp"
else
    echo "=== IntelliJ IDEA Community MCP ==="
    echo "  Project     : ${PROJECT}"
    echo "  MCP URL     : http://localhost:${LISTEN_PORT}/sse"
fi
echo "  IDEA        : ${IDE_DIR}"
echo "  Display     : $([ "${IDEA_HEADLESS}" = "true" ] && echo headless || echo 'Xvfb :99')"
echo "  Config      : ${IDE_CONFIG_PATH}"
echo "  Launcher log: ${LOG_FILE}"
echo "  IDEA log    : ${IDEA_LOG}"
echo ""

# ── Start Xvfb (virtual display) unless running headless ──────────────────────
# IDEA supports -Djava.awt.headless=true natively (the default). When
# IDEA_HEADLESS=false, Xvfb provides a virtual X11 display that satisfies AWT
# without real hardware — the same way PyCharm always runs.
XVFB_PID=""
HEADLESS_ARGS=()
if [ "${IDEA_HEADLESS}" = "true" ]; then
    # JAVA_TOOL_OPTIONS is read by the JVM before application startup code, making
    # it the most reliable way to set java.awt.headless=true.
    export JAVA_TOOL_OPTIONS="-Djava.awt.headless=true"
    HEADLESS_ARGS=("-Djava.awt.headless=true")
else
    pkill -x Xvfb 2>/dev/null || true
    sleep 0.2
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
    export DISPLAY=:99
    Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
    XVFB_PID=$!
    echo "Xvfb started (PID=${XVFB_PID}, DISPLAY=${DISPLAY})"
    sleep 1
fi

# ── Start IDEA in background ──────────────────────────────────────────────────
OPEN_ARGS=()
if [ "${MCP_STEROID}" = "true" ]; then
    echo ">>> Launching IDEA (no project; mcp-steroid handles project opening)"
else
    OPEN_ARGS=("open" "${PROJECT}")
    echo ">>> Launching IDEA with open-project AppStarter (project: ${PROJECT})"
fi

# -Dmcp.steroid.review.mode=NEVER: run mcp-steroid's steroid_execute_code without the manual
# code-review gate. Its default (ALWAYS) opens a review.kts and waits for a human to approve every
# execution; here there is no reviewer, so each call hangs until mcp.steroid.review.timeout (600s).
# NEVER auto-approves all executions — required for autonomous use (harmless when mcp-steroid absent).
"${IDE_DIR}/bin/idea.sh" \
    "${HEADLESS_ARGS[@]+"${HEADLESS_ARGS[@]}"}" \
    -Didea.platform.prefix=Idea \
    -Didea.trust.all.projects=true \
    -Dide.no.platform.update=true \
    -Dmcp.steroid.review.mode=NEVER \
    -Didea.system.path="${IDE_SYSTEM_PATH}" \
    -Didea.config.path="${IDE_CONFIG_PATH}" \
    -Didea.plugins.path="${IDE_DIR}/plugins" \
    "${OPEN_ARGS[@]+"${OPEN_ARGS[@]}"}" > "${LOG_FILE}" 2>&1 &
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
        kill -0 "${IDEA_PID}" 2>/dev/null || { echo "IDEA exited."; break; }
        sleep 1
    done
    kill -9 "${IDEA_PID}" 2>/dev/null || true
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

    if (( i % 30 == 0 )); then
        if [ -f "${IDEA_LOG}" ]; then
            echo "--- idea.log tail at ${i}s ---"
            tail -10 "${IDEA_LOG}"
        fi
    fi

    sleep 1
done

# ── Start socat bridge ────────────────────────────────────────────────────────
socat TCP-LISTEN:${BRIDGE_PORT},fork,reuseaddr TCP:127.0.0.1:${LISTEN_PORT} &
SOCAT_PID=$!
echo ">>> socat bridge: 0.0.0.0:${BRIDGE_PORT} -> 127.0.0.1:${LISTEN_PORT} (PID=${SOCAT_PID})"

# ── Keep the container alive until IDEA exits ─────────────────────────────────
echo "Container running. Waiting for IDEA to exit..."
wait "${IDEA_PID}"
