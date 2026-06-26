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
| `list_server_mcp_tools` | List all MCP tools exposed by a running IdeGYM server |
| `call_server_mcp_tool` | Call an MCP tool on a running IdeGYM server by name |

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

## Server-side MCP

Every running IdeGYM server exposes its own MCP endpoint at `/mcp`. The orchestrator's
`list_server_mcp_tools` and `call_server_mcp_tool` tools bridge to this endpoint, so agents can
discover and invoke server-side tools without an additional network hop.

### How it works

Each IdeGYM server runs a FastMCP proxy that reads upstream declarations from
`/etc/idegym/mcp-upstreams.d/*.json`. Each file names an upstream:

```json
{"url": "http://localhost:6315/mcp"}
```

The file's stem (e.g. `pycharm`) becomes the namespace prefix in the proxy. All tools from that
upstream are reachable through the server's `/mcp` endpoint with the namespace prefix applied
(e.g. `pycharm_steroid_open_project`).

Image-build plugins declare their MCP upstream via `get_mcp_upstream()`. `Image.to_spec()` writes
the config file automatically. See [Plugins](plugins.md) for details.

### Built-in server tools

Every IdeGYM server exposes these tools at `/mcp` regardless of which plugins are installed:

| Tool | Description |
|------|-------------|
| `create_file` | Create a new file with the given content |
| `edit_file` | Replace a line range in an existing file (1-indexed, inclusive) |
| `patch_file` | Apply a unified diff patch to an existing file |

Additional tools appear when image-build plugins declare MCP upstreams (e.g. IDE plugins, mcp-steroid).
See [Tools Reference](tools.md) for the full catalogue.

### Listing tools on a running server

```python
tools_result = await mcp.call_tool(
    "list_server_mcp_tools",
    {
        "request": {
            "client_id": client_id,
            "server_id": server_id,
        },
    },
)
tools = tools_result.structured_content["tools"]
for tool in tools:
    print(tool["name"], "-", tool.get("description", ""))
```

Each entry has `name`, `description` (may be absent), and `input_schema` (JSON Schema object).

### Calling a tool on a running server

```python
result = await mcp.call_tool(
    "call_server_mcp_tool",
    {
        "request": {
            "client_id": client_id,
            "server_id": server_id,
            "tool_name": "pycharm_steroid_list_projects",
            "arguments": {},
        },
    },
)
content = result.structured_content["content"]   # list of MCP content items
is_error = result.structured_content["is_error"]
```

`content` is a list of MCP content items (text, images, etc.) as returned by the tool. Check
`is_error` to distinguish tool-level errors from transport errors.

---

## JetBrains IDE MCP Endpoints

The `Idea` and `PyCharm` image-build plugins install a JetBrains MCP server into the container.
Two modes are available: the **bundled JetBrains plugin** and **mcp-steroid**.

### Bundled JetBrains MCP plugin

When `open_project=True` and a `Project` plugin precedes the IDE plugin in the build pipeline,
the bundled JetBrains MCP plugin is activated. It binds to `127.0.0.1:64342` (loopback only).

At runtime, the startup script starts a socat bridge on `0.0.0.0:64343`, making the server
reachable from outside the container. For standalone Docker deployments:

```bash
docker run -p 64343:64343 <image>
# connect to: http://localhost:64343/sse  or  http://localhost:64343/stream
```

Both `/sse` (legacy) and `/stream` (newer versions) endpoints support the same MCP protocol.
The startup scripts automatically detect which endpoint is available.

The plugin is declared as an MCP upstream on port 64342. When accessed through
`list_server_mcp_tools` / `call_server_mcp_tool`, its tools are namespaced as `idea_*` or
`pycharm_*` depending on the plugin type.

### mcp-steroid

[mcp-steroid](https://github.com/jonnyzzz/mcp-steroid) is an alternative JetBrains plugin that
runs its MCP server inside the IDE JVM, providing direct access to the IntelliJ Platform API:
project model, semantic index, PSI tree, test runner, debugger, and VCS layer.

Enable it by passing `mcp_steroid=True` to the `Idea` or `PyCharm` plugin:

```python
from idegym.image.builder import Image
from idegym.plugins.pycharm.image import PyCharm

image = (
    Image(base="...")
    .plugin(PyCharm(version="2026.1.1", mcp_steroid=True))
)
```

When `mcp_steroid=True`:

- mcp-steroid binds to `127.0.0.1:6315`; a socat bridge re-listens on `0.0.0.0:6316`
- `get_mcp_upstream()` advertises port 6315 instead of 64342
- If `open_project=False` (or no `Project` plugin in the pipeline), the IDE starts without a
  project — agents can open one at runtime via the `steroid_open_project` tool
- The plugin version can be pinned with `mcp_steroid_version` (format: `X.Y` or `X.Y.Z`, optionally with a `-HASH` suffix — e.g. `0.94.0-8682a5ce` or `0.100-409f23a2`)

See [Tools Reference](tools.md#mcp-steroid-tools) for the full list of mcp-steroid tools and resources.
