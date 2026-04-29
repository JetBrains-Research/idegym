# Client Library

The `idegym-client` package provides a Python async client for interacting with the IdeGYM orchestrator.
It handles authentication, client registration, server lifecycle management, and all tool operations.

## Installation

The client is part of the IdeGYM workspace. If you have the repository set up:

```sh
uv sync --all-packages --all-extras --all-groups
```

Or install from PyPI (once published):

```sh
uv add idegym-client
```

## Quick Example

```python
import asyncio
from idegym.client.client import IdeGYMClient

async def main():
    async with IdeGYMClient(
        orchestrator_url="https://idegym.yourdomain.com",
        name="my-training-run",
        namespace="idegym",
    ) as client:
        async with client.with_server(
            image_tag="registry.example.com/my-env:latest",
            server_name="my-server",
        ) as server:
            result = await server.execute_bash(script="echo hello")
            print(result.stdout)  # → hello

asyncio.run(main())
```

## `IdeGYMClient`

```python
from idegym.client.client import IdeGYMClient
```

The main entry point. Must be used as an async context manager — `async with IdeGYMClient(...) as client` —
which registers the client with the orchestrator on entry and deregisters it on exit (stopping all
associated servers).

### Constructor

```python
IdeGYMClient(
    orchestrator_url: str,
    name: str,
    namespace: str,
    nodes_count: int = 0,
    auth: Optional[BasicAuth] = None,
    client_id: Optional[str] = None,
    heartbeat_interval_in_seconds: int = 60,
    request_timeout_in_seconds: int = 60,
    otel_config: Optional[OTELConfig] = None,
)
```

| Parameter | Description |
|-----------|-------------|
| `orchestrator_url` | URL of the orchestrator API (e.g., `https://idegym.yourdomain.com`) |
| `name` | Client name used for resource quota assignment |
| `namespace` | Kubernetes namespace where servers are created |
| `nodes_count` | Number of nodes to reserve for this client (default: `0`) |
| `auth` | `BasicAuth(username, password)`. Defaults to `IDEGYM_AUTH_USERNAME` / `IDEGYM_AUTH_PASSWORD` environment variables |
| `client_id` | If provided, attaches to an existing client session without sending heartbeats |
| `heartbeat_interval_in_seconds` | How often to send liveness heartbeats to the orchestrator (default: `60`) |
| `request_timeout_in_seconds` | Default HTTP request timeout (default: `60`) |
| `otel_config` | OpenTelemetry tracing configuration |

**Authentication via environment variables:**

```sh
export IDEGYM_AUTH_USERNAME=admin
export IDEGYM_AUTH_PASSWORD=your-password
```

### `with_server(...)` — async context manager

Start a server and yield an `IdeGYMServer`, then stop or finish it on exit.

```python
async with client.with_server(
    image_tag="registry.example.com/my-env:latest",
    server_name="my-server",
    namespace=None,                          # defaults to client namespace
    runtime_class_name="gvisor",
    run_as_root=False,
    resources=None,                          # V1ResourceRequirements
    node_selector=None,
    server_start_wait_timeout_in_seconds=60,
    reuse_strategy=ServerReuseStrategy.RESET,
    close_action=ServerCloseAction.FINISH,   # FINISH or STOP
) as server:
    ...
```

| Parameter | Description |
|-----------|-------------|
| `image_tag` | OCI image to run as the server |
| `server_name` | Kubernetes name for the server deployment (must be a valid k8s object name) |
| `runtime_class_name` | Runtime class for the pod (e.g., `"gvisor"` for sandboxing) |
| `run_as_root` | Run the container as root (default: `False`) |
| `resources` | Kubernetes resource requests/limits |
| `node_selector` | Node affinity labels |
| `server_start_wait_timeout_in_seconds` | How long to wait for the server pod to become ready |
| `reuse_strategy` | What to do if a server with this name already exists: `NONE` (recreate the server from scratch), `RESTART` (restart the server), `RESET` (reset project state), `CHECKPOINT` (restore from checkpoint; not yet supported) |
| `close_action` | `FINISH` — release the server but leave it running for the next client; `STOP` — stop and delete the server |

### `start_server(...)` / `stop_server(...)` / `finish_server(...)`

Explicit lifecycle control without the context manager:

```python
server = await client.start_server(image_tag=..., server_name=..., ...)

# Work with the server...

await client.finish_server(server)  # release without stopping
# or
await client.stop_server(server)    # stop and delete
```

### `build_and_push_images(path, timeout, poll_interval)` — image builds

Submit a YAML image definition for Kaniko build and wait for completion:

```python
from pathlib import Path

summary = await client.build_and_push_images(
    path=Path("image.yaml"),
    timeout=600,        # seconds; None = no timeout
    poll_interval=10,   # seconds between status polls
)

if summary.failed_jobs > 0:
    raise RuntimeError(f"Build failed: {summary.jobs_results}")

image_tag = summary.jobs_results[0].tag
```

### `health_check()`

```python
response = await client.health_check()
print(response.status)  # → "healthy"
```

---

## `IdeGYMServer`

Returned by `client.with_server()` or `client.start_server()`. Provides all operations on a running
server environment.

### `execute_bash(script, ...)`

Run a bash script in the environment:

```python
result = await server.execute_bash(
    script="python -c 'print(1+1)'",
    command_timeout=600.0,                  # seconds
    graceful_termination_timeout=2.0,       # seconds
    request_timeout=None,                   # HTTP timeout (uses client default)
)

print(result.exit_code)   # 0
print(result.stdout)      # "2\n"
print(result.stderr)      # ""
```

### `reset_project(...)`

Reset the project to its original state (re-extracts the project archive):

```python
result = await server.reset_project(
    reset_timeout=600.0,
    graceful_termination_timeout=2.0,
)
```

### `create_file(file_path, content, ...)`

Create a new file at the given path:

```python
result = await server.create_file(
    file_path="/home/devuser/hello.py",
    content='print("hello world")\n',
)
```

### `edit_file(file_path, start_line, end_line, new_content, ...)`

Replace a range of lines in an existing file (1-indexed, inclusive):

```python
result = await server.edit_file(
    file_path="/home/devuser/hello.py",
    start_line=1,
    end_line=1,
    new_content='print("goodbye world")\n',
)
```

### `patch_file(file_path, patch, ...)`

Apply a unified diff patch to a file:

```python
result = await server.patch_file(
    file_path="/home/devuser/hello.py",
    patch="--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-print...",
)
```

### `restart_server(...)`

Restart the server pod (preserves the same image and configuration):

```python
response = await server.restart_server(
    server_start_wait_timeout_in_seconds=60,
)
```

### `forward(method, path, body, ...)` — generic plugin endpoint call

An escape hatch for calling plugin-provided endpoints that do not have a typed wrapper:

```python
from idegym.api.tools.bash import BashCommandRequest

response = await server.forward(
    method="POST",
    path="tools/bash",
    body=BashCommandRequest(command="echo hello", timeout=30.0),
)
# response is the parsed JSON dict
```

```python
server.forward(
    method: str,
    path: str,
    body: Optional[BaseModel] = None,
    request_timeout: Optional[int] = None,
    polling_config: Optional[PollingConfig] = None,
) -> dict[str, Any]
```

The `path` is relative to the server's API base (`/api/`). For typed access to plugin operations,
prefer the attribute-style API described below.

---

## Plugin-Provided Operations

Installed plugins can extend `IdeGYMServer` with typed operation objects. Each plugin package
registers an operations class via the `idegym.plugins.client` entry point group. When
`IdeGYMServer` is constructed, it loads all installed client plugins and attaches each as an
attribute under the entry point name.

### PyCharm operations (`server.pycharm`)

When the `pycharm` plugin is installed and the image includes a PyCharm plugin, `server.pycharm`
is attached automatically:

```python
health = await server.pycharm.health()
# → {"status": "ok", ...}
```

The `pycharm` attribute is only present if the `idegym-plugin-defaults` package is installed and
the `pycharm` client entry point loads successfully. Accessing it on a server whose image was built
without the PyCharm plugin will raise `AttributeError`.

### Checking for a plugin

```python
if hasattr(server, "pycharm"):
    health = await server.pycharm.health()
```

---

> **See also:** [Plugin Architecture](plugins.md) — full guide for writing plugins that extend the
> server and client with new endpoints and typed operations.

---

## Reward Operations

`IdeGYMServer` exposes reward signals used for RL training evaluation:

### `compilation_reward(compilation_script, ...)`

Run a compilation check and get a pass/fail reward:

```python
result = await server.compilation_reward(
    compilation_script="cd /home/devuser/project && python -m py_compile main.py",
    compilation_timeout=600.0,
)
print(result.success)   # True / False
```

### `setup_reward(setup_check_script, ...)`

Run a setup verification script:

```python
result = await server.setup_reward(
    setup_check_script="cd /project && pip check",
    setup_timeout=600.0,
)
print(result.success)
```

### `test_reward(test_script, ...)`

Run a test suite and get a structured report:

```python
result = await server.test_reward(
    test_script="cd /project && python -m pytest --tb=short",
    test_timeout=600.0,
)
print(result.passed)    # number of passing tests
print(result.failed)    # number of failing tests
print(result.output)    # full test output
```

---

## WebSocket / OpenEnv Access

For environments that implement the OpenEnv protocol, use `server.openenv_url` to get the
WebSocket base URL and connect with an OpenEnv client:

```python
url = server.openenv_url
# → "https://idegym.yourdomain.com/api/ws-forward/<client_id>/<server_id>"

# Pass to an OpenEnv-compatible client:
env_client = MyOpenEnvClient(base_url=url)
```

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `IDEGYM_AUTH_USERNAME` | Orchestrator username | — |
| `IDEGYM_AUTH_PASSWORD` | Orchestrator password | — |
| `IDEGYM_OTEL_SERVICE_NAME` | OpenTelemetry service name for traces | auto-generated |
| `IDEGYM_OTEL_TRACING_ENDPOINT` | OTLP trace export endpoint | JetBrains internal Tempo |
| `IDEGYM_OTEL_TRACING_TIMEOUT` | Trace export timeout in seconds | `10` |
| `IDEGYM_OTEL_TRACING_AUTH_USERNAME` | Trace export auth username | — |
| `IDEGYM_OTEL_TRACING_AUTH_PASSWORD` | Trace export auth password | — |

---

## Error Handling

All client methods raise `RuntimeError` on failure (server start failed, build job failed, etc.).
HTTP errors from the orchestrator are surfaced as exceptions with the response details included in
the message.

```python
try:
    async with client.with_server(image_tag="nonexistent:latest") as server:
        ...
except RuntimeError as e:
    print(f"Failed: {e}")
```

---

## Complete Example with Rewards

```python
import asyncio
from pathlib import Path
from idegym.client.client import IdeGYMClient
from idegym.image.builder import Image
from idegym.image.plugins import User, Project

async def train_step(client: IdeGYMClient, image_tag: str, patch: str) -> float:
    """Apply a patch and return a test-pass reward."""
    async with client.with_server(
        image_tag=image_tag,
        server_name="train-server",
        reuse_strategy="RESET",  # reset project on each episode
        server_start_wait_timeout_in_seconds=300,
    ) as server:
        # Apply the model's proposed change
        await server.patch_file(
            file_path="/home/devuser/project/src/main.py",
            patch=patch,
        )

        # Evaluate
        result = await server.test_reward(
            test_script="cd /home/devuser/project && python -m pytest -q",
            test_timeout=120.0,
        )

        total = result.passed + result.failed
        return result.passed / total if total > 0 else 0.0


async def main():
    async with IdeGYMClient(
        orchestrator_url="https://idegym.yourdomain.com",
        name="rl-training",
        namespace="idegym",
        nodes_count=4,  # reserve 4 nodes for parallel episodes
    ) as client:
        image_tag = "registry.example.com/my-env:latest"
        reward = await train_step(client, image_tag, patch="--- ...")
        print(f"Reward: {reward:.2f}")

asyncio.run(main())
```
