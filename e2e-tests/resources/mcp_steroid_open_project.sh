# Call steroid_open_project via mcp-steroid MCP server.
# PROJECT_PATH must be exported by the caller; defaults to /root/work.
# Stdout contains the steroid_open_project JSON response.
MCP_URL="http://localhost:6315/mcp"
: "${PROJECT_PATH:=/root/work}"
curl -s --max-time 10 \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"idegym-e2e","version":"0.1.0"}}}' \
    "${MCP_URL}" > /dev/null 2>&1
curl -s --max-time 30 \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"steroid_open_project\",\"arguments\":{\"project_path\":\"${PROJECT_PATH}\",\"task_id\":\"e2e-test\",\"reason\":\"E2E test: verify project opens via mcp-steroid\"}}}" \
    "${MCP_URL}"
