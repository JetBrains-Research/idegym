# Tools Reference

IdeGYM servers expose HTTP endpoints and MCP tools. This page covers every tool category
available on a running server, how to call each one, and what plugins are required.

To discover the tools available on a specific running server at runtime, use the orchestrator MCP
`list_server_mcp_tools` tool. See [MCP Server — Server-side MCP](mcp.md#server-side-mcp).

---

## Default Tools

Every IdeGYM server includes the default tools plugin. These tools are available on all images
regardless of which IDE or additional plugins are installed.

The file tools (`create_file`, `edit_file`, `patch_file`) are also exposed as MCP tools on the
server's `/mcp` endpoint, so they can be called via `call_server_mcp_tool` without going through
the HTTP API.

### Bash

Execute a bash script inside the container.

**Endpoint:** `POST /api/tools/bash`

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `command` | string | — | Bash script to execute |
| `timeout` | float | 600.0 | Maximum execution time in seconds |
| `graceful_termination_timeout` | float | 2.0 | Seconds to wait for graceful process exit before SIGKILL |

**Response:**

| Field | Type | Description |
|-------|------|-------------|
| `stdout` | string | Standard output |
| `stderr` | string | Standard error |
| `exit_code` | int | Exit code returned by the process |

**Via orchestrator MCP (`run_bash_command`):**

```python
command = await mcp.call_tool(
    "run_bash_command",
    {
        "request": {
            "client_id": client_id,
            "server_id": server_id,
            "command": "python -c 'import sys; print(sys.version)'",
            "command_timeout": 30.0,
        },
    },
)
```

**Via Python client:**

```python
response = await server.execute_bash("python -c 'import sys; print(sys.version)'")
print(response.stdout)
```

### Create File

Create a new file with given content.

**Endpoint:** `POST /api/tools/create-file` · **MCP tool:** `create_file`

**Request:**

| Field | Type | Description |
|-------|------|-------------|
| `file_path` | string | Absolute path where the file will be created |
| `content` | string | File content |

**Via server MCP:**

```python
result = await mcp.call_tool(
    "call_server_mcp_tool",
    {
        "request": {
            "client_id": client_id,
            "server_id": server_id,
            "tool_name": "create_file",
            "arguments": {"request": {"file_path": "/root/work/hello.py", "content": "print('hello')\n"}},
        },
    },
)
```

### Edit File

Replace a range of lines in an existing file.

**Endpoint:** `POST /api/tools/edit-file` · **MCP tool:** `edit_file`

**Request:**

| Field | Type | Description |
|-------|------|-------------|
| `file_path` | string | Absolute path to the file |
| `start_line` | int | First line to replace (1-indexed) |
| `end_line` | int | Last line to replace (inclusive) |
| `new_content` | string | Replacement content for the specified range |

### Patch File

Apply a unified diff patch to a file.

**Endpoint:** `POST /api/tools/patch-file` · **MCP tool:** `patch_file`

**Request:**

| Field | Type | Description |
|-------|------|-------------|
| `file_path` | string | Absolute path to the file |
| `patch` | string | Unified diff patch to apply |

---

## IDE Inspection (`inspect.sh`)

The `Idea` and `PyCharm` plugins expose an inspection endpoint backed by the JetBrains command-line
inspector (`inspect.sh`). It runs the full IntelliJ static analysis pipeline and writes results to
a directory inside the container.

**Requires:** `Idea` or `PyCharm` plugin in the image build pipeline.

### IDEA Endpoint

**Endpoint:** `POST /idea/inspect`

### PyCharm Endpoint

**Endpoint:** `POST /pycharm/inspect`

### Request

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `project_path` | string | — | Absolute path to the project directory inside the container |
| `profile_path` | string | — | Absolute path to an inspection profile XML (e.g. `.idea/inspectionProfiles/Project_Default.xml`) |
| `output_dir` | string | — | Directory where result files will be written (created if absent) |
| `changes_only` | bool | `false` | Only inspect locally changed files (`-changes`) |
| `directory` | string | `null` | Limit scope to this subdirectory (`-d`) |
| `format` | string | `"xml"` | Output format: `"xml"` or `"json"` |
| `verbosity` | int | `0` | Verbosity level 0–2 (`-v0` / `-v1` / `-v2`) |
| `timeout` | float | 600.0 | Maximum seconds to wait for `inspect.sh` |

### Response

| Field | Type | Description |
|-------|------|-------------|
| `output_dir` | string | Directory containing the result files |
| `exit_code` | int | Exit code from `inspect.sh` (0 = success) |
| `stdout` | string | Standard output |
| `stderr` | string | Standard error |

Result files (XML or JSON) are written to `output_dir` inside the container. Read them afterwards
with a bash tool call:

```python
results = await server.execute_bash(f"cat {output_dir}/*.xml")
```

### Via Python Client

```python
from idegym.client.client import IdeGYMClient

client = IdeGYMClient(...)
server = await client.start_server(...)

# IDEA
response = await server.idea.inspect(
    project_path="/root/work",
    profile_path="/root/work/.idea/inspectionProfiles/Project_Default.xml",
    output_dir="/tmp/inspection-results",
    format="xml",
    timeout=300.0,
)
print("exit_code:", response.exit_code)

# Read results
results = await server.execute_bash("cat /tmp/inspection-results/*.xml")
print(results.stdout)
```

Replace `server.idea` with `server.pycharm` when using the PyCharm plugin.

### Via Orchestrator MCP

```python
import json

forward = await mcp.call_tool(
    "forward_request",
    {
        "request": {
            "client_id": client_id,
            "server_id": server_id,
            "path": "idea/inspect",
            "method": "POST",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "project_path": "/root/work",
                    "profile_path": "/root/work/.idea/inspectionProfiles/Project_Default.xml",
                    "output_dir": "/tmp/inspection-results",
                    "format": "xml",
                }
            ),
        },
    },
)
```

---

## mcp-steroid Tools

[mcp-steroid](https://github.com/jonnyzzz/mcp-steroid) is a JetBrains plugin that runs an MCP
server inside the IDE JVM. It provides direct access to the IntelliJ Platform API: project model,
semantic index, PSI tree, test runner, debugger, and VCS layer.

**Requires:** `Idea` or `PyCharm` plugin with `mcp_steroid=True`.

```python
from idegym.plugins.pycharm.image import PyCharm

PyCharm(version="2026.1.1", mcp_steroid=True)
```

**Ports:**
- Inside container: `127.0.0.1:6315` (MCP endpoint: `/mcp`)
- External bridge: `0.0.0.0:6316`
- MCP transport: streamable HTTP (not SSE)

**Namespace in server MCP proxy:** tools are accessible through the server's `/mcp` endpoint
prefixed with the plugin type name (e.g. `pycharm_steroid_open_project` when the PyCharm plugin
declares the upstream).

### MCP Tools

mcp-steroid exposes 9 MCP tools:

| Tool | Description |
|------|-------------|
| `steroid_open_project` | Open a project in the IDE by path |
| `steroid_list_projects` | List projects currently known to the IDE |
| `steroid_list_windows` | List open editor windows |
| `steroid_execute_code` | Execute Kotlin code in the IDE JVM with full IntelliJ Platform API access |
| `steroid_take_screenshot` | Take a screenshot of the IDE window |
| `steroid_input` | Send keyboard or mouse input to the IDE |
| `steroid_action_discovery` | Discover available IDE actions |
| `steroid_fetch_resource` | Fetch an MCP resource by URI (LSP, refactoring, debugging, testing, VCS) |
| `steroid_execute_feedback` | Provide feedback on executed operations |

### MCP Resources

mcp-steroid also exposes 58+ MCP resources covering:

- **LSP** — diagnostics, hover, go-to-definition, find-references
- **Refactoring** — rename, move, extract
- **Debugging** — breakpoints, step execution, variable inspection
- **Testing** — run/inspect test results
- **VCS** — git status, diff, history

### Accessing mcp-steroid via Orchestrator MCP

Use `list_server_mcp_tools` and `call_server_mcp_tool` to reach mcp-steroid tools through the
orchestrator without a direct network connection to the container.

List all available tools (includes mcp-steroid tools prefixed with the plugin namespace):

```python
tools_result = await mcp.call_tool(
    "list_server_mcp_tools",
    {"request": {"client_id": client_id, "server_id": server_id}},
)
for tool in tools_result.structured_content["tools"]:
    print(tool["name"])
# pycharm_steroid_open_project
# pycharm_steroid_list_projects
# ...
```

Open a project:

```python
result = await mcp.call_tool(
    "call_server_mcp_tool",
    {
        "request": {
            "client_id": client_id,
            "server_id": server_id,
            "tool_name": "pycharm_steroid_open_project",
            "arguments": {"path": "/root/work"},
        },
    },
)
```

Execute Kotlin in the IDE JVM:

```python
result = await mcp.call_tool(
    "call_server_mcp_tool",
    {
        "request": {
            "client_id": client_id,
            "server_id": server_id,
            "tool_name": "pycharm_steroid_execute_code",
            "arguments": {
                "code": "project.name",
            },
        },
    },
)
```

### Accessing mcp-steroid Directly (Docker)

For standalone Docker deployments, the socat bridge exposes port 6316:

```bash
docker run -p 6316:6316 <image>
# MCP endpoint: http://localhost:6316/mcp
```

```python
from fastmcp import Client

async with Client("http://localhost:6316/mcp") as mcp:
    tools = await mcp.list_tools()
    result = await mcp.call_tool("steroid_list_projects", {})
```

---

## Discovering Tools at Runtime

Use the orchestrator MCP to discover which tools are available on a running server without
knowing in advance which plugins are installed:

```python
tools_result = await mcp.call_tool(
    "list_server_mcp_tools",
    {"request": {"client_id": client_id, "server_id": server_id}},
)
available = {t["name"] for t in tools_result.structured_content["tools"]}

if "pycharm_steroid_open_project" in available:
    print("mcp-steroid is available on this server")
```

See [MCP Server](mcp.md) for the full orchestrator MCP reference.
