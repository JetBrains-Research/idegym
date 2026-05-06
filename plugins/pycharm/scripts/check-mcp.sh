#!/usr/bin/env bash
# Checks MCP server availability by probing /stream, /mcp, and /sse endpoints.
# Can be used standalone as a CLI tool or sourced to use the check_mcp_endpoint function.
#
# Standalone usage:
#   check-mcp.sh [BASE_URL]
#
# Sourced usage:
#   source check-mcp.sh
#   if check_mcp_endpoint "http://localhost:64342/stream"; then
#       echo "MCP ready!"
#   fi
#
# Arguments (standalone):
#   BASE_URL  - Base URL of the MCP server (default: http://localhost:64342)
#
# Exit codes (standalone):
#   0 - MCP server is ready and responding
#   1 - MCP detected but initialize failed
#   2 - No working MCP endpoint found

set -euo pipefail

# Function to check a single MCP endpoint
check_mcp_endpoint() {
    local url="$1"

    # Determine endpoint type based on URL
    if [[ "$url" =~ /sse$ ]]; then
        # SSE is a streaming GET endpoint - just check for HTTP 200
        # Ktor doesn't handle HEAD for SSE, so we use GET with max-time
        local http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "${url}" 2>/dev/null || true)
        if [ "${http_code}" = "200" ]; then
            return 0
        fi
        return 1
    else
        # For /stream and /mcp endpoints, try JSON-RPC initialize

        # Fast probe - check for jsonrpc response
        if ! curl -s --max-time 3 "${url}" 2>/dev/null | grep -q '"jsonrpc"'; then
            return 1
        fi

        # Try proper MCP initialize
        local init_resp=$(curl -s --max-time 3 "${url}" \
            -H "Content-Type: application/json" \
            -H "Accept: application/json, text/event-stream" \
            -d '{
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "idegym-check",
                        "version": "0.1.0"
                    }
                }
            }' 2>/dev/null)

        if echo "${init_resp}" | grep -q '"result"'; then
            return 0
        fi
        return 1
    fi
}

# Only run standalone logic if script is executed (not sourced)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    BASE_URL="${1:-http://localhost:64342}"
    ENDPOINTS=("/stream" "/mcp" "/sse")

    echo "Checking MCP server at: $BASE_URL"
    echo

    for ep in "${ENDPOINTS[@]}"; do
        url="$BASE_URL$ep"
        echo "→ Probing $url"

        if [[ "$url" =~ /sse$ ]]; then
            # SSE endpoint - check HTTP status
            http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "$url" 2>/dev/null || true)
            if [ "${http_code}" = "200" ]; then
                echo "  ✅ SSE endpoint is READY (HTTP 200)"
                echo
                echo "🎉 Working MCP endpoint: $url"
                exit 0
            else
                echo "  ✖ SSE endpoint not ready (HTTP ${http_code:-000})"
            fi
        else
            # JSON-RPC endpoint - try initialize
            if ! curl -s --max-time 3 "$url" 2>/dev/null | grep -q '"jsonrpc"'; then
                echo "  ✖ Not an MCP endpoint"
            else
                echo "  ✔ MCP-like response detected"

                init_resp=$(curl -s --max-time 3 "$url" \
                    -H "Content-Type: application/json" \
                    -H "Accept: application/json, text/event-stream" \
                    -d '{
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {
                                "name": "idegym-check",
                                "version": "0.1.0"
                            }
                        }
                    }' 2>/dev/null)

                if echo "$init_resp" | grep -q '"result"'; then
                    echo "  ✅ MCP is READY (initialize succeeded)"
                    echo
                    echo "🎉 Working MCP endpoint: $url"
                    exit 0
                else
                    echo "  ⚠ MCP detected but initialize failed"
                    echo "  Response: $init_resp"
                fi
            fi
        fi
        echo
    done

    echo "❌ No working MCP endpoint found"
    exit 2
fi
