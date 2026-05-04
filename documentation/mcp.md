# MCP Server

IdeGYM exposes an MCP server for tool-based access to orchestrator operations. This is useful for agents and other
clients that discover and call tools instead of using the REST API directly.

The MCP server is available from the orchestrator at:

```text
/mcp
```

Use the same host as the HTTP API. For example:

- Local e2e deployment: `http://idegym-local.test/mcp`
- Remote deployment: `https://idegym.yourdomain.com/mcp`

The MCP server does not keep separate state. It is a thin layer over the orchestrator: tools call the same client,
server, forwarding, async operation, and Kaniko build handlers as the REST API.

---

## Authentication

The MCP app is mounted inside the orchestrator FastAPI service. In the manifests in this repository, the orchestrator
does not install an application-level Basic Auth middleware and does not set `IDEGYM_AUTH_USERNAME` or
`IDEGYM_AUTH_PASSWORD`.

If your deployment protects the orchestrator behind an ingress, reverse proxy, or other gateway, connect to `/mcp`
with the same credentials required for the HTTP API. If the orchestrator is exposed without external authentication,
omit the `auth` argument.

```python
import asyncio

import httpx
from fastmcp import Client


async def main():
    auth = httpx.BasicAuth(username="admin", password="your-password")
    async with Client("https://idegym.yourdomain.com/mcp", auth=auth, timeout=600.0) as mcp:
        tools = await mcp.list_tools()
        print([tool.name for tool in tools])


asyncio.run(main())
```

---

## Tool Model

FastMCP exposes IdeGYM request models under a top-level `request` argument. For example, `register_client`
uses the same fields as the REST `POST /api/clients` request:

```python
result = await mcp.call_tool(
    "register_client",
    {
        "request": {
            "name": "agent-run-1",
            "namespace": "idegym",
            "nodes_count": 1,
        },
    },
)
client_id = result.structured_content["id"]
```

Long-running tools return an operation ID. Poll `get_operation_status` until the operation reaches `SUCCEEDED`
or a terminal failure state. When the operation succeeds, `result` contains the JSON-serialized final response.

```python
import asyncio


async def wait_for_operation(mcp, operation_id: int, poll_interval: float = 1.0):
    while True:
        status = await mcp.call_tool(
            "get_operation_status",
            {"request": {"operation_id": operation_id}},
        )
        operation_status = status.structured_content["status"]

        if operation_status == "SUCCEEDED":
            return status.structured_content
        if operation_status in {"FAILED", "CANCELLED"}:
            raise RuntimeError(f"Operation {operation_id} ended with {operation_status}: {status.structured_content}")

        await asyncio.sleep(poll_interval)
```

---

## Available Tools

| Tool | Description |
|------|-------------|
| `register_client` | Create a client record, optionally with node pre-provisioning |
| `stop_client` | Stop a client and delete its alive server resources |
| `finish_client` | Mark a client and its alive servers reusable without deleting resources |
| `start_server` | Start a server pod or reuse a matching finished server |
| `stop_server` | Stop a server and delete its Kubernetes resources |
| `finish_server` | Mark a server reusable without deleting Kubernetes resources |
| `restart_server` | Restart server pods and wait for readiness |
| `build_images_from_yaml` | Start Kaniko image build jobs from image-builder YAML |
| `get_operation_status` | Read the status and result of an async operation |
| `get_job_status` | Read the status and image tag for a Kaniko build job |
| `forward_request` | Forward an HTTP request to a running IdeGYM server |
| `run_bash_command` | Execute a bash script on a running IdeGYM server |

`forward_request.path` is a server-internal path without a leading slash, for example `api/tools/bash`. The
orchestrator forwards it to `http://{server-service}/{path}` inside the Kubernetes cluster.

---

## Server Lifecycle Example

Start a server with reuse enabled:

```python
import json

start = await mcp.call_tool(
    "start_server",
    {
        "request": {
            "client_id": client_id,
            "namespace": "idegym",
            "image_tag": "registry.example.com/my-env:latest",
            "server_name": "my-server",
            "runtime_class_name": "gvisor",
            "reuse_strategy": "RESTART",
        },
    },
)
start_status = await wait_for_operation(mcp, start.structured_content["operation_id"])

start_response = json.loads(start_status["result"])
server_id = start_response["server_id"]
```

`start_status["result"]` contains the JSON-serialized `StartServerResponse`.

Run a command on the server:

```python
command = await mcp.call_tool(
    "run_bash_command",
    {
        "request": {
            "client_id": client_id,
            "server_id": server_id,
            "command": "python -c 'print(\"hello\")'",
            "command_timeout": 60.0,
        },
    },
)
command_operation_id = command.structured_content["async_operation_id"]
command_status = await wait_for_operation(mcp, command_operation_id)

forward_response = json.loads(command_status["result"])
bash_response = json.loads(forward_response["body"])
print(bash_response["stdout"])
print(bash_response["stderr"])
print(bash_response["exit_code"])
```

To keep a server available for reuse, call `finish_server`. A later `start_server` call with matching parameters and
`reuse_strategy: "RESTART"` or `"RESET"` can reuse the same server.

```python
await mcp.call_tool(
    "finish_server",
    {
        "request": {
            "client_id": client_id,
            "namespace": "idegym",
            "server_id": server_id,
        },
    },
)
```

To delete the Kubernetes resources instead, call `stop_server`.

---

## Nested MCP Servers

Nested MCP server support is planned. The current MCP server is hosted by the orchestrator and exposes orchestrator
operations only. Future nested support can be documented here without changing the orchestrator API reference.
