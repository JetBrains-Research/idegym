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
| `cwd` | string or null | `null` | Working directory; a relative path resolves against the project directory |
| `env` | object | `{}` | Environment variables added to the command's environment |
| `user` | string or null | `null` | Run the command as this user via `runuser` |
| `timeout` | float | 600.0 | Maximum execution time in seconds |
| `graceful_termination_timeout` | float | 2.0 | Seconds to wait for graceful process exit before SIGKILL |
| `max_output_bytes` | integer or null | 1048576 | Maximum retained bytes per stream; `null` retains complete output |
| `strip_output` | bool | `false` | Trim leading and trailing whitespace from `stdout` and `stderr` |

**Response:**

| Field | Type | Description |
|-------|------|-------------|
| `stdout` | string | Retained standard output |
| `stderr` | string | Retained standard error |
| `exit_code` | int | Exit code returned by the process |

The default limit applies independently to stdout and stderr. Truncated output keeps its
beginning and end and includes a marker with the number of omitted bytes; the marker itself
is outside the configured limit. Set `max_output_bytes` to `null` only when complete output
is required and its size is trusted. IdeGYM still drains all produced output until the command
finishes or reaches its execution timeout so subprocess pipes cannot deadlock.

#### How the script is run

The script is written to a temp file inside the container and executed as `bash <file>`. There
is no practical size limit: passing it as a `bash -c` argument used to cap it at the kernel's
`MAX_ARG_STRLEN` of 128 KiB, and an oversized script failed with a bare `E2BIG`.

A file rather than bash's stdin is deliberate. A script read from stdin is consumed
incrementally, so any command inside it that reads stdin — `cat`, `read`, an interactive
installer — would swallow the rest of the script. Running from a file leaves the command's own
stdin alone.

The temp file is removed once the command finishes, including when it times out.

Before the script, IdeGYM sources a bundled init file that sets up the shell environment. It is
joined to your script with `;`, not `&&`, so it cannot change what your script means: every
statement runs exactly as written, and your own `&&` and `||` behave normally. The prefix stays
on the same line, so a bash error still reports the line number you wrote. An empty script is a
no-op that exits 0.

#### Per-command context

Without `cwd`, `env` and `user` the only way to set context is to write it into the script —
`cd -- … || exit 1`, one `export` per variable, a `runuser` wrapper — which every client would
have to generate correctly, and which spends the script's own size budget.

```python
result = await server.execute_bash(
    "python -m pytest -q",
    cwd="tests",  # relative to the project directory
    env={"PYTHONHASHSEED": "0"},  # merged over the cleaned environment
    user="devuser",  # requires the server to run as root
)
```

The environment the command starts from is the server's own, with IdeGYM-internal entries
stripped; `env` is merged over it, so a name that already exists is overridden. Values passed
this way never enter the command text, which means they are not written to the command log —
prefer it to an `export` line for anything sensitive.

`user` runs the script through `runuser --preserve-environment`, so it needs the server
container to be running as root. Without it the command runs as the server's own user.

#### Output fidelity

Within the retained window, output is returned exactly as the command wrote it. Nothing is
trimmed, so a trailing newline that belongs to a `git diff` survives and `printf 'x'` stays
distinguishable from `printf '  x  '`. Set `strip_output` to `true` if you would otherwise call
`.strip()` on the result yourself.

Bytes that are not valid UTF-8 are replaced with `U+FFFD` rather than failing the request, so
`cat` on a binary file or a compiler emitting latin-1 diagnostics still returns the command's
real exit code. The response is JSON text: to move binary data out of the container intact,
encode it inside the command (`base64 -w0 …`) or use the
[file endpoints](client.md).

#### What gets logged

The server never writes a command or its output to the log at INFO. An INFO record carries the
shape of the call only — the command's length on the way in, then the exit code and the byte
count of each stream on the way out.

The command itself and excerpts of both streams are logged at DEBUG, and the command passes
through a redaction step first that replaces the value of every `export NAME=...` assignment
with `<redacted>`. Raise the level with `IDEGYM_LOG_LEVEL=DEBUG` when you need to see what a
sandbox actually ran, and be aware that anything a script sets by other means — an inline
`NAME=value cmd` prefix, a heredoc, a literal in an argument — is not redacted.

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
            "max_output_bytes": 1048576,
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

### Upload File Chunk

Write one base64-encoded chunk of raw bytes into a file. This is the binary-safe way in: the
orchestrator stores and replays a forwarded request as JSON text, so base64 is what survives the
round trip.

**Endpoint:** `POST /api/tools/file/upload`

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | string | — | Absolute path to write to; missing parent directories are created |
| `content_base64` | string | — | Base64-encoded bytes to write at `offset` |
| `offset` | int | 0 | Byte offset in the file at which to write this chunk |
| `truncate` | bool | `true` | Cut the file off at the end of this chunk once written |

**Response:** `file_path`, `bytes_written`, `size` (file size after the write).

`truncate` is what keeps a re-upload honest: with it on, writing a shorter file cannot leave the
tail of a longer previous one behind. Turn it off for an out-of-order write. Writing past the
current end of the file leaves a hole of zero bytes, as `lseek` does.

### Download File Chunk

Read one base64-encoded chunk of raw bytes from a file.

**Endpoint:** `POST /api/tools/file/download`

**Request:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | string | — | Absolute path to read from |
| `offset` | int | 0 | Byte offset to read from |
| `length` | int | 1048576 | Maximum number of raw bytes to read |

**Response:** `file_path`, `offset`, `content_base64`, `bytes_read`, `size` (total file size),
and `eof` (true when this chunk reaches the end). A missing path returns 404; a directory
returns 400.

**Via Python client:** the client wraps both endpoints in `upload_file` / `upload_bytes` /
`download_file` / `download_bytes`, which do the chunking for you. See
[Binary file transfer](client.md#binary-file-transfer).

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
