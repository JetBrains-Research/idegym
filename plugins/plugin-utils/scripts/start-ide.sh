#!/usr/bin/env bash
# Shared container entrypoint for the JetBrains IDE plugins (installed as /usr/local/bin/start-ide,
# run by supervisord). Launch the IDE, wait for its MCP endpoint, then expose it via a socat bridge.
# Bash because this is mainly process/signal orchestration (background IDE + Xvfb + socat + a cleanup
# trap), not logic.
#
# The IDE-specific bits are baked in as env vars by each plugin's install template:
#   IDE_NAME               display name for banners            (e.g. "IntelliJ IDEA")
#   IDE_DIR                installation directory              (e.g. /opt/idea)
#   IDE_LAUNCHER           launcher under ${IDE_DIR}/bin       (e.g. idea.sh)
#   IDE_LOG_FILE           launcher stdout/stderr log          (e.g. /tmp/idea.log)
#   IDE_LAUNCH_ARGS        extra IDE-specific launcher flags   (space-separated, may be empty)
#   IDE_SUPPORTS_HEADLESS  IDEA honours IDEA_HEADLESS; PyCharm is always Xvfb (true/false)
#
# Runtime behaviour is selected by two env vars (see each plugin's docstring for details):
#   IDEA_HEADLESS  true (default) → headless AWT; false → Xvfb virtual display (IDEA only)
#   MCP_STEROID    false (default) → open a project + bundled MCP plugin; true → mcp-steroid mode
#
# Other env vars (all have defaults): IDE_SYSTEM_PATH (/tmp/ide-system), IDE_CONFIG_PATH
# (/tmp/ide-config), WAIT_SECONDS (300), IDEGYM_PROJECT_ROOT (/root/work), MCP_PORT/BRIDGE_PORT
# (64342/64343), MCP_STEROID_PORT/MCP_STEROID_BRIDGE_PORT (6315/6316).

set -euo pipefail

IDE_NAME="${IDE_NAME:-IDE}"
IDE_DIR="${IDE_DIR:-/opt/idea}"
IDE_LAUNCHER="${IDE_LAUNCHER:-idea.sh}"
IDE_SUPPORTS_HEADLESS="${IDE_SUPPORTS_HEADLESS:-false}"
MCP_STEROID="${MCP_STEROID:-false}"
IDEA_HEADLESS="${IDEA_HEADLESS:-true}"
IDE_SYSTEM_PATH="${IDE_SYSTEM_PATH:-/tmp/ide-system}"
IDE_CONFIG_PATH="${IDE_CONFIG_PATH:-/tmp/ide-config}"
# Non-headless (Xvfb) startup + indexing is slower than headless, so allow a generous
# MCP-endpoint budget. Headless still breaks out early on success.
WAIT_SECONDS="${WAIT_SECONDS:-300}"
LOG_FILE="${IDE_LOG_FILE:-/tmp/ide.log}"
IDE_LOG="${IDE_SYSTEM_PATH}/log/idea.log"

# Effective headless mode: only when the IDE supports it AND it was not turned off.
HEADLESS=false
if [ "${IDE_SUPPORTS_HEADLESS}" = "true" ] && [ "${IDEA_HEADLESS}" = "true" ]; then
    HEADLESS=true
fi

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
    echo "=== ${IDE_NAME} + mcp-steroid (no project) ==="
    echo "  mcp-steroid : http://localhost:${LISTEN_PORT}/mcp"
else
    echo "=== ${IDE_NAME} MCP ==="
    echo "  Project     : ${PROJECT}"
    echo "  MCP URL     : http://localhost:${LISTEN_PORT}/sse"
fi
echo "  IDE         : ${IDE_DIR}"
echo "  Display     : $([ "${HEADLESS}" = "true" ] && echo headless || echo 'Xvfb :99')"
echo "  Config      : ${IDE_CONFIG_PATH}"
echo "  Launcher log: ${LOG_FILE}"
echo "  IDE log     : ${IDE_LOG}"
echo ""

# ── Start Xvfb (virtual display) unless running headless ──────────────────────
XVFB_PID=""
HEADLESS_ARGS=()
if [ "${HEADLESS}" = "true" ]; then
    # JAVA_TOOL_OPTIONS is read by the JVM before application startup code, making it the most
    # reliable way to set java.awt.headless=true. Append (don't clobber) so any flags the runtime
    # already exported (proxy, TLS, ...) are preserved.
    export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:+${JAVA_TOOL_OPTIONS} }-Djava.awt.headless=true"
    HEADLESS_ARGS=("-Djava.awt.headless=true")
else
    # Xvfb is only installed for images that need a display (PyCharm always, IDEA with
    # headless=False). Fail early with a clear message if it is somehow missing.
    if ! command -v Xvfb >/dev/null 2>&1; then
        echo "ERROR: a virtual display is required but Xvfb is not installed in this image." >&2
        echo "       (IDEA bundles Xvfb only when built with headless=False.)" >&2
        exit 1
    fi
    pkill -x Xvfb 2>/dev/null || true
    sleep 0.2
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
    export DISPLAY=:99
    Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
    XVFB_PID=$!
    echo "Xvfb started (PID=${XVFB_PID}, DISPLAY=${DISPLAY})"
    sleep 1
fi

# ── Start the IDE in background ───────────────────────────────────────────────
OPEN_ARGS=()
if [ "${MCP_STEROID}" = "true" ]; then
    echo ">>> Launching ${IDE_NAME} (no project; mcp-steroid handles project opening)"
else
    OPEN_ARGS=("open" "${PROJECT}")
    echo ">>> Launching ${IDE_NAME} with open-project AppStarter (project: ${PROJECT})"
fi

# -Dmcp.steroid.review.mode=NEVER: run mcp-steroid's steroid_execute_code without the manual
# code-review gate. Its default (ALWAYS) opens a review.kts and waits for a human to approve every
# execution; here there is no reviewer, so each call hangs until mcp.steroid.review.timeout (600s).
# NEVER auto-approves all executions — required for autonomous use (harmless when mcp-steroid absent).
read -ra LAUNCH_ARGS <<< "${IDE_LAUNCH_ARGS:-}" || true
"${IDE_DIR}/bin/${IDE_LAUNCHER}" \
    "${HEADLESS_ARGS[@]+"${HEADLESS_ARGS[@]}"}" \
    "${LAUNCH_ARGS[@]+"${LAUNCH_ARGS[@]}"}" \
    -Didea.trust.all.projects=true \
    -Dide.no.platform.update=true \
    -Dmcp.steroid.review.mode=NEVER \
    -Didea.system.path="${IDE_SYSTEM_PATH}" \
    -Didea.config.path="${IDE_CONFIG_PATH}" \
    -Didea.plugins.path="${IDE_DIR}/plugins" \
    "${OPEN_ARGS[@]+"${OPEN_ARGS[@]}"}" > "${LOG_FILE}" 2>&1 &
IDE_PID=$!
echo "${IDE_NAME} started (PID=${IDE_PID})"

# ── Graceful shutdown on SIGTERM / SIGINT ─────────────────────────────────────
SOCAT_PID=""
cleanup() {
    echo ""
    echo "Shutting down ${IDE_NAME} (PID=${IDE_PID})..."
    kill "${SOCAT_PID}" 2>/dev/null || true
    kill "${IDE_PID}" 2>/dev/null || true
    for _ in $(seq 1 10); do
        kill -0 "${IDE_PID}" 2>/dev/null || { echo "${IDE_NAME} exited."; break; }
        sleep 1
    done
    kill -9 "${IDE_PID}" 2>/dev/null || true
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

    if ! kill -0 "${IDE_PID}" 2>/dev/null; then
        echo "ERROR: ${IDE_NAME} process (PID=${IDE_PID}) exited unexpectedly." >&2
        echo "--- last 30 lines of ${LOG_FILE} ---" >&2
        tail -30 "${LOG_FILE}" >&2
        if [ -f "${IDE_LOG}" ]; then
            echo "--- last 50 lines of ${IDE_LOG} ---" >&2
            tail -50 "${IDE_LOG}" >&2
        fi
        exit 1
    fi

    if [[ "${i}" -eq "${WAIT_SECONDS}" ]]; then
        echo "ERROR: MCP endpoint not reachable after ${WAIT_SECONDS}s." >&2
        echo "--- last 30 lines of ${LOG_FILE} ---" >&2
        tail -30 "${LOG_FILE}" >&2
        if [ -f "${IDE_LOG}" ]; then
            echo "--- last 50 lines of ${IDE_LOG} ---" >&2
            tail -50 "${IDE_LOG}" >&2
        fi
        exit 1
    fi

    if (( i % 30 == 0 )); then
        if [ -f "${IDE_LOG}" ]; then
            echo "--- ${IDE_LOG} tail at ${i}s ---"
            tail -10 "${IDE_LOG}"
        fi
    fi

    sleep 1
done

# ── Start socat bridge ────────────────────────────────────────────────────────
socat TCP-LISTEN:${BRIDGE_PORT},fork,reuseaddr TCP:127.0.0.1:${LISTEN_PORT} &
SOCAT_PID=$!
echo ">>> socat bridge: 0.0.0.0:${BRIDGE_PORT} -> 127.0.0.1:${LISTEN_PORT} (PID=${SOCAT_PID})"

# ── Keep the container alive until the IDE exits ──────────────────────────────
echo "Container running. Waiting for ${IDE_NAME} to exit..."
wait "${IDE_PID}"
