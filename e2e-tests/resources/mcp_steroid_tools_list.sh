# Send MCP initialize (response discarded) then tools/list.
# Stdout contains the tools/list JSON response.
MCP_URL="http://localhost:6315/mcp"
curl -s --max-time 10 \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"idegym-e2e","version":"0.1.0"}}}' \
    "${MCP_URL}" > /dev/null 2>&1
curl -s --max-time 10 \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
    "${MCP_URL}"
