# Poll the MCP SSE endpoint every 3s for up to 180s (60 iterations).
# Exits 0 with "SUCCESS" on the first 200 response; exits 1 after timeout.
# Used by IDEA and PyCharm inspect/plugin tests that share the same 180s budget.
for i in $(seq 1 60); do
    http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 \
        "http://localhost:64342/sse" 2>/dev/null || true)
    if [ "$http_code" = "200" ]; then
        echo "SUCCESS: MCP server ready after $((i * 3))s"
        exit 0
    fi
    echo "... waiting for MCP ($((i * 3))s elapsed, last HTTP code: $http_code)"
    sleep 3
done
echo "TIMEOUT: MCP server not reachable after 180s"
echo "=== IDE log (last 30 lines) ==="
cat "/tmp/ide-system/log/idea.log" 2>/dev/null | tail -30 || echo "(log not found)"
echo "=== socat/IDE processes ==="
ps aux 2>/dev/null | grep -E 'socat|idea' | grep -v grep || echo "(none)"
exit 1
