# Call steroid_list_projects via mcp-steroid MCP server.
# Stdout contains the steroid_list_projects JSON response.
MCP_URL="http://localhost:6315/mcp"
curl -s --max-time 10 \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"steroid_list_projects","arguments":{}}}' \
    "${MCP_URL}"
