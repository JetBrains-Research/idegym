# Call steroid_input via mcp-steroid MCP server to send a keystroke.
# Sends Enter (\n) to dismiss modal dialogs.
MCP_URL="http://localhost:6315/mcp"
curl -s --max-time 10 \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"steroid_input","arguments":{"text":"\n"}}}' \
    "${MCP_URL}"
