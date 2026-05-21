# Call steroid_take_screenshot via mcp-steroid MCP server.
# Requires PROJECT_NAME env var.
# TASK_ID defaults to a timestamp.
# REASON defaults to "e2e test screenshot".
# Stdout contains the steroid_take_screenshot JSON response.
MCP_URL="http://localhost:6315/mcp"
TASK_ID="${TASK_ID:-$(date +%s)}"
REASON="${REASON:-e2e test screenshot}"
WINDOW_ID_ARG="${WINDOW_ID:+,\"window_id\":\"${WINDOW_ID}\"}"
curl -s --max-time 15 \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"tools/call\",\"params\":{\"name\":\"steroid_take_screenshot\",\"arguments\":{\"project_name\":\"${PROJECT_NAME}\",\"task_id\":\"${TASK_ID}\",\"reason\":\"${REASON}\"${WINDOW_ID_ARG}}}}" \
    "${MCP_URL}"
