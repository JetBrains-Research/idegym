#!/usr/bin/env bash
set -euo pipefail

log_file="/root/.cache/JetBrains/PyCharmCE${PYCHARM_VERSION}/log/idea.log"

echo ">>> Starting Xvfb on :99"
Xvfb :99 -screen 0 1024x768x24 -nolisten tcp &
sleep 1
echo ">>> Starting openbox window manager"
DISPLAY=:99 openbox --sm-disable &
sleep 1

echo ">>> Starting PyCharm Community ${PYCHARM_VERSION} (no CLI arg — plugin opens ${IDEGYM_PROJECT_ROOT})"
# Start without CLI arg so the Welcome Screen shows; this allows appStarted() to fire.
# The open-project plugin then opens IDEGYM_PROJECT_ROOT from inside appStarted().
pycharm.sh >/tmp/pycharm-stdout.log 2>&1 &
PYCHARM_PID=$!

# If a "Data Sharing" consent dialog appears, dismiss it by clicking the button.
# PyCharm's data-sharing consent dialog blocks the EDT until dismissed.
# The dialog appears ~30-50s into startup; poll every 3s.
# We click at 48% from left, 89% from top of the dialog — the position of
# the primary button (verified empirically on PyCharm 2024.3 / 1024x768).
(sleep 30; for _i in $(seq 1 60); do
    wid=$(DISPLAY=:99 xdotool search --name "Data Sharing" 2>/dev/null | head -1)
    if [ -n "$wid" ]; then
        echo ">>> Found Data Sharing dialog (window $wid), clicking to dismiss..."
        # Read geometry without activating (activation may reorder Z-stacking)
        geo=$(DISPLAY=:99 xdotool getwindowgeometry "$wid" 2>/dev/null)
        pos_x=$(echo "$geo" | awk '/Position/ {split($2, a, ","); print int(a[1])}')
        pos_y=$(echo "$geo" | awk '/Position/ {split($2, a, ","); gsub(/[^0-9].*/, "", a[2]); print int(a[2])}')
        geo_w=$(echo "$geo" | awk '/Geometry/ {split($2, a, "x"); print int(a[1])}')
        geo_h=$(echo "$geo" | awk '/Geometry/ {split($2, a, "x"); print int(a[2])}')
        btn_y=$(( pos_y + geo_h * 89 / 100 ))
        echo "    dialog at ${pos_x},${pos_y} size ${geo_w}x${geo_h}, scanning buttons at y=${btn_y}"
        # Scan horizontally across the button row; dismiss on first hit
        for pct in 20 30 40 50 60 70 80; do
            btn_x=$(( pos_x + geo_w * pct / 100 ))
            DISPLAY=:99 xdotool mousemove "$btn_x" "$btn_y"
            sleep 0.05
            DISPLAY=:99 xdotool click 1
            sleep 0.15
            still=$(DISPLAY=:99 xdotool search --name "Data Sharing" 2>/dev/null | head -1)
            if [ -z "$still" ]; then
                echo ">>> Dialog dismissed by click at ${btn_x},${btn_y} (${pct}%)"
                break
            fi
        done
    fi
    sleep 3
done) &

echo ">>> Waiting up to 5min for PyCharm to open the project..."
for i in $(seq 1 60); do
    sleep 5
    # workspace.xml is created by PyCharm when it actually opens a project session
    if [ -f "${IDEGYM_PROJECT_ROOT}/.idea/workspace.xml" ]; then
        echo ">>> workspace.xml created after $((i * 5))s — project opened!"
        break
    fi
    if [ -f "$log_file" ]; then
        if grep -qE "(OpenProjectOnStartup|ProjectManagerImpl.*open|projectOpened.*${IDEGYM_PROJECT_ROOT##*/})" "$log_file" 2>/dev/null; then
            echo ">>> Project open signal detected in idea.log after $((i * 5))s"
            echo ">>> Waiting up to 60s for workspace.xml to appear..."
            for j in $(seq 1 12); do
                sleep 5
                if [ -f "${IDEGYM_PROJECT_ROOT}/.idea/workspace.xml" ]; then
                    echo ">>> workspace.xml appeared after additional $((j * 5))s"
                    break
                fi
            done
            break
        fi
    fi
    echo "    ... still waiting ($((i * 5))s elapsed)"
done

echo ""
echo "=== Thread dump (jstack) ==="
jstack "$PYCHARM_PID" 2>/dev/null || echo "(jstack failed — trying kill -3)"
kill -3 "$PYCHARM_PID" 2>/dev/null || true
sleep 2

echo ""
echo "=== consent file written by PyCharm ==="
cat /root/.local/share/JetBrains/consentOptions/accepted 2>/dev/null || echo "(not created)"

echo ""
echo "=== project open check ==="
project_name="${IDEGYM_PROJECT_ROOT##*/}"
if [ -f "${IDEGYM_PROJECT_ROOT}/.idea/workspace.xml" ]; then
    echo "SUCCESS: workspace.xml exists — PyCharm opened the project!"
elif [ -f "$log_file" ] && grep -qE "exit dumb mode \[${project_name}\]" "$log_file" 2>/dev/null; then
    echo "SUCCESS: 'exit dumb mode [${project_name}]' in idea.log — project fully loaded!"
elif [ -f "$log_file" ] && grep -qE "OpenProjectOnStartup.*opening project at|UnindexedFilesIndexer.*Finished for ${project_name}" "$log_file" 2>/dev/null; then
    echo "SUCCESS: project open + indexing complete signals found in idea.log"
else
    echo "ABSENT: no project open signals detected"
fi

echo ""
echo "=== .idea directory contents ==="
ls -la "${IDEGYM_PROJECT_ROOT}/.idea/" 2>/dev/null || echo "(no .idea directory)"

echo ""
echo "=== PyCharm process check ==="
ps aux | grep -i pycharm | grep -v grep || echo "(no pycharm process found)"

echo ""
echo "=== idea.log (full) ==="
if [ -f "$log_file" ]; then
    echo "Log path: $log_file"
    cat "$log_file"
else
    # Fall back: find any idea.log in the cache
    found=$(find /root/.cache/JetBrains -name "idea.log" 2>/dev/null | head -1)
    if [ -n "$found" ]; then
        echo "Log path: $found"
        cat "$found"
    else
        echo "(no idea.log found yet)"
        echo ""
        echo "=== PyCharm stdout ==="
        cat /tmp/pycharm-stdout.log 2>/dev/null || true
    fi
fi
