#!/usr/bin/env bash
# Starts PyCharm Community with MCP server enabled, waits until the MCP
# endpoint is reachable, then blocks (keeping the container alive).
#
# PyCharm CE does not support java.awt.headless=true; Xvfb is used to provide a
# virtual X11 display. Config files pre-created in the image under the XDG
# default (~/.config/JetBrains/PyCharmCE<version>/) are picked up automatically.
#
# Log locations:
#   /tmp/pycharm.log                    - IDE launcher stdout/stderr
#   ${IDE_SYSTEM_PATH}/log/idea.log     - PyCharm internal log (idea.log)
#
# Environment variables used at runtime (all have defaults):
#   IDEGYM_PROJECT_ROOT  – project to open                      (default: /root/work)
#   PYCHARM_DIR          – PyCharm installation directory        (default: /opt/pycharm)
#   IDE_SYSTEM_PATH      – PyCharm system/cache/log directory   (default: /tmp/ide-system)
#   IDE_CONFIG_PATH      – PyCharm config directory             (default: /tmp/ide-config)
#   MCP_PORT             – port the MCP SSE endpoint listens on  (default: 64342)
#   BRIDGE_PORT          – port socat exposes on 0.0.0.0         (default: 64343)
#   WAIT_SECONDS         – max seconds to wait for MCP endpoint  (default: 300)

set -euo pipefail

PROJECT="${IDEGYM_PROJECT_ROOT:-/root/work}"
PYCHARM_DIR="${PYCHARM_DIR:-/opt/pycharm}"
IDE_SYSTEM_PATH="${IDE_SYSTEM_PATH:-/tmp/ide-system}"
IDE_CONFIG_PATH="${IDE_CONFIG_PATH:-/tmp/ide-config}"
MCP_PORT="${MCP_PORT:-64342}"
BRIDGE_PORT="${BRIDGE_PORT:-64343}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
MCP_URL="http://localhost:${MCP_PORT}/sse"
LOG_FILE="/tmp/pycharm.log"
PYCHARM_LOG="${IDE_SYSTEM_PATH}/log/idea.log"

mkdir -p "${IDE_SYSTEM_PATH}" "${IDE_CONFIG_PATH}/options"

echo "=== PyCharm Community MCP ==="
echo "  Project     : ${PROJECT}"
echo "  PyCharm     : ${PYCHARM_DIR}"
echo "  MCP URL     : ${MCP_URL}"
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
# JVM properties passed directly to pycharm.sh.
# The JetBrains launcher recognises -D* / -X* flags and routes them to the JVM
# before any application arguments.
echo ">>> Launching PyCharm with open-project AppStarter (project: ${PROJECT})"
"${PYCHARM_DIR}/bin/pycharm.sh" \
    -Didea.trust.all.projects=true \
    -Didea.system.path="${IDE_SYSTEM_PATH}" \
    -Didea.config.path="${IDE_CONFIG_PATH}" \
    -Didea.plugins.path="${PYCHARM_DIR}/plugins" \
    -Dide.no.platform.update=true \
    -Dide.show.tips.on.startup.default.value=false \
    -Djb.consents.confirmation.enabled=false \
    open "${PROJECT}" > "${LOG_FILE}" 2>&1 &
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

# ── Wait for the MCP SSE endpoint ─────────────────────────────────────────────
echo "Waiting for MCP endpoint at ${MCP_URL} (timeout: ${WAIT_SECONDS}s)..."
for i in $(seq 1 "${WAIT_SECONDS}"); do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "${MCP_URL}" 2>/dev/null || true)
    if [ "${HTTP_CODE}" = "200" ]; then
        echo ">>> MCP server ready (${i}s)"
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
        echo "ERROR: MCP endpoint not reachable after ${WAIT_SECONDS}s." >&2
        echo "--- last 30 lines of ${LOG_FILE} ---" >&2
        tail -30 "${LOG_FILE}" >&2
        if [ -f "${PYCHARM_LOG}" ]; then
            echo "--- last 50 lines of ${PYCHARM_LOG} ---" >&2
            tail -50 "${PYCHARM_LOG}" >&2
        fi
        exit 1
    fi

    # Print pycharm log tail every 30 s so it's visible in container logs
    if (( i % 30 == 0 )); then
        if [ -f "${PYCHARM_LOG}" ]; then
            echo "--- pycharm log tail at ${i}s ---"
            tail -10 "${PYCHARM_LOG}"
        fi
    fi

    sleep 1
done

# ── Start socat bridge ────────────────────────────────────────────────────────
socat TCP-LISTEN:${BRIDGE_PORT},fork,reuseaddr TCP:127.0.0.1:${MCP_PORT} &
SOCAT_PID=$!
echo ">>> socat bridge: 0.0.0.0:${BRIDGE_PORT} -> 127.0.0.1:${MCP_PORT} (PID=${SOCAT_PID})"

# ── Keep the container alive until PyCharm exits ──────────────────────────────
echo "Container running. Waiting for PyCharm to exit..."
wait "${PYCHARM_PID}"
