# Poll the mcp-steroid MCP endpoint every 5s for up to 300s (60 iterations).
# mcp-steroid exposes a streamable HTTP MCP server on 127.0.0.1:6315/mcp.
# Exits 0 with "SUCCESS" on first successful MCP initialize; exits 1 after timeout.
MCP_URL="http://localhost:6315/mcp"
for i in $(seq 1 60); do
    resp=$(curl -s --max-time 5 \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"idegym-e2e","version":"0.1.0"}}}' \
        "${MCP_URL}" 2>/dev/null || true)
    if echo "${resp}" | grep -q '"result"'; then
        echo "SUCCESS: mcp-steroid ready after $((i * 5))s"
        exit 0
    fi
    echo "... waiting for mcp-steroid ($((i * 5))s elapsed)"
    sleep 5
done
echo "TIMEOUT: mcp-steroid not reachable after 300s"
echo "=== IDE log (last 30 lines) ==="
cat "/tmp/ide-system/log/idea.log" 2>/dev/null | tail -30 || echo "(log not found)"
echo "=== IDE processes ==="
ps aux 2>/dev/null | grep -E 'java|idea' | grep -v grep || echo "(none)"
exit 1
