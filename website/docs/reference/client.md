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
    namespace=None,  # defaults to client namespace
    runtime_class_name="gvisor",
    run_as_root=False,
    resources=None,  # KubernetesResources
    node_selector=None,
    server_start_wait_timeout_in_seconds=60,
    reuse_strategy=ServerReuseStrategy.RESET,
    close_action=ServerCloseAction.FINISH,  # FINISH or STOP
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
| `reuse_strategy` | What to do if a server with this name already exists: `NONE` (recreate the server from scratch), `RESTART` (restart the server), `RESET` (reset project state) |
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
    timeout=600,  # seconds; None = no timeout
    poll_interval=10,  # seconds between status polls
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
    command_timeout=600.0,  # seconds
    graceful_termination_timeout=2.0,  # seconds
    request_timeout=None,  # HTTP timeout (uses client default)
    max_output_bytes=1048576,  # retained bytes for each stream; None disables truncation
)

print(result.exit_code)  # 0
print(result.stdout)  # "2\n"
print(result.stderr)  # ""
```

The default retains the beginning and end of stdout and stderr independently and inserts a
marker when bytes are omitted. Use `max_output_bytes=None` when a caller must parse complete,
trusted output. Output is returned verbatim within that window; pass `strip_output=True` if you
want surrounding whitespace trimmed.

`cwd`, `env` and `user` set per-command context, so you do not have to build `cd`, `export` and
`runuser` into the script yourself:

```python
result = await server.execute_bash(
    "python -m pytest -q",
    cwd="tests",  # relative paths resolve against the project directory
    env={"PYTHONHASHSEED": "0"},  # merged over the server's cleaned environment
    user="devuser",  # requires the server to run as root
)
```

Values passed through `env` never enter the command text, so they are not written to the
command log — prefer it to an `export` line for anything sensitive. See
[Tools — Per-command context](tools.md#per-command-context).

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

### Binary file transfer

`create_file` and `patch_file` carry text. To move bytes — an archive, a wheel, a compiled
artifact, a database fixture — use the transfer methods, which chunk the content as base64 over
the ordinary forwarding path. That path stores and replays a request as JSON text, so base64 is
what makes the round trip lossless; raw bytes would not survive it.

```python
# Upload a local file into the container, then read it back byte-for-byte.
size = await server.upload_file("./fixtures/repo.tar.gz", "/root/work/repo.tar.gz")
await server.download_file("/root/work/out/results.db", "./results.db")

# In-memory variants for smaller payloads.
await server.upload_bytes("/root/work/blob.bin", b"\x00\xff")
blob = await server.download_bytes("/root/work/blob.bin")
```

| Method | Returns | Description |
|--------|---------|-------------|
| `upload_file(local_path, file_path, chunk_bytes=..., ...)` | `int` | Stream a local file into the container; returns the resulting size |
| `upload_bytes(file_path, data, chunk_bytes=..., ...)` | `int` | Write bytes already in memory; returns the resulting size |
| `download_file(file_path, local_path, chunk_bytes=..., ...)` | `int` | Stream a container file to disk; returns the byte count |
| `download_bytes(file_path, chunk_bytes=..., ...)` | `bytes` | Read a container file into memory |

`chunk_bytes` is the number of raw bytes per request and defaults to 1 MiB; base64 inflates that
to roughly 1.4 MiB on the wire. The file-based variants read and write one chunk at a time, so a
transfer is bounded by disk rather than by client memory. Uploads are sequential and each chunk
truncates the file at its own end, so an interrupted upload leaves a short file rather than a
file with a stale tail. Downloading a path that does not exist fails with a 404.

Prefer these over base64 through the bash tool: they are not bounded by the size of a single
shell script, and the payload never reaches the command log.

### `restart_server(...)`

Restart the server pod (preserves the same image and configuration):

```python
response = await server.restart_server(
    server_start_wait_timeout_in_seconds=60,
)
```

### `list_capabilities()` — loaded plugin list

Return the list of server plugins running in the container:

```python
result = await server.list_capabilities()
print(result.plugins)  # → ["tools", "rewards"]
```

This calls `GET /api/idegym-servers/{id}/capabilities` on the orchestrator, which proxies to
`GET /api/capabilities` on the server. The response reflects the contents of
`/etc/idegym/plugins.json` written at image build time.

```python
result: CapabilitiesResponse = await server.list_capabilities()
```

---

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

`server.pycharm` is attached automatically when `idegym-plugins[pycharm]` is installed
in the **local** Python environment and its `idegym.plugins.client` entry point loads successfully.
This is a purely local check — it is independent of whether the running server image was built with
the PyCharm plugin.

#### `inspect(...)`

Runs the JetBrains built-in `inspect.sh` script on the server and returns an `InspectResponse`:

```python
result = await server.pycharm.inspect(
    project_path="/root/work",
    profile_path="/root/work/.idea/inspectionProfiles/Default.xml",
    output_dir="/tmp/inspect-out",
    timeout=300.0,
)
assert result.exit_code == 0
# Read result files from the container:
xml = await server.execute_bash("cat /tmp/inspect-out/*.xml")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project_path` | `str` | — | Absolute path to the project inside the container |
| `profile_path` | `str` | — | Absolute path to an inspection profile XML file |
| `output_dir` | `str` | — | Directory where result files will be written |
| `changes_only` | `bool` | `False` | Only inspect locally changed files (`-changes`) |
| `directory` | `Optional[str]` | `None` | Limit scope to a subdirectory (`-d`) |
| `format` | `str` | `"xml"` | Output format: `"xml"` or `"json"` |
| `verbosity` | `int` | `0` | Verbosity level 0–2 |
| `timeout` | `float` | `600.0` | Maximum seconds for `inspect.sh` to run |
| `request_timeout` | `Optional[int]` | `None` | HTTP request timeout override (seconds) |

**Note:** `inspect.sh` runs in batch/headless mode — no Xvfb or display server is required for
inspection. Xvfb is only needed when PyCharm opens projects interactively (`open_project=True`).

### IDEA operations (`server.idea`)

`server.idea` is attached automatically when `idegym-plugins[idea]` is installed in the
**local** Python environment. It provides the same `inspect()` interface as `server.pycharm`:

```python
result = await server.idea.inspect(
    project_path="/root/work",
    profile_path="/root/work/.idea/inspectionProfiles/Default.xml",
    output_dir="/tmp/inspect-out",
)
```

IntelliJ IDEA supports true headless mode (`java.awt.headless=true`) — no Xvfb is needed by
default. Pass `Idea(headless=False)` to run the IDE against a virtual display (Xvfb on `:99`),
the same way PyCharm runs, for workloads that need a real AWT toolkit.

### Checking for a plugin at runtime

Use `server.list_capabilities()` to get the definitive list of plugins loaded in the running container,
then check membership before calling plugin-specific methods:

```python
caps = await server.list_capabilities()
# → CapabilitiesResponse(plugins=["tools", "rewards", "pycharm"])

if "pycharm" in caps.plugins and hasattr(server, "pycharm"):
    result = await server.pycharm.inspect(
        project_path="/root/work",
        profile_path="/root/work/.idea/inspectionProfiles/Default.xml",
        output_dir="/tmp/inspect-out",
    )
```

`list_capabilities()` calls `GET /api/idegym-servers/{id}/capabilities` on the orchestrator, which
proxies to `GET /api/capabilities` on the server container and returns the contents of
`/etc/idegym/plugins.json` — the file written at image build time that controls which plugins are
loaded at startup.

`hasattr(server, "pycharm")` still guards against the local package not being installed, but
`caps.plugins` is the authoritative runtime check for whether the server image supports a plugin.

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
print(result.success)  # True / False
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
print(result.passed)  # number of passing tests
print(result.failed)  # number of failing tests
print(result.output)  # full test output
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
| `IDEGYM_OTEL_TRACING_ENDPOINT` | OTLP trace export endpoint | — (tracing off) |
| `IDEGYM_OTEL_TRACING_TIMEOUT` | Trace export timeout in seconds | `10` |
| `IDEGYM_OTEL_TRACING_AUTH_USERNAME` | Trace export auth username | — |
| `IDEGYM_OTEL_TRACING_AUTH_PASSWORD` | Trace export auth password | — |

### Tracing

Tracing is off unless you give it an endpoint — there is no default collector, so a client
constructed with no OpenTelemetry configuration exports nothing. Turn it on with the environment
variable above, or with an explicit config:

```python
from idegym.api.config import OTELConfig, TracingConfig

client = IdeGYMClient(
    orchestrator_url="https://idegym.yourdomain.com",
    name="my-training-run",
    namespace="idegym",
    otel_config=OTELConfig(
        service_name="my-training-run",
        tracing=TracingConfig(endpoint="https://collector.internal/v1/traces"),
    ),
)
```

An explicit `otel_config` is used as given; the environment variables are only consulted when
none is passed.

---

## Error Handling

An HTTP failure raises a subclass of `IdeGYMHTTPError`, chosen by status code. The exception
carries `status_code`, `body`, `method` and `url` as attributes, so a retry policy can branch on
the failure instead of parsing the message.

```python
from idegym.client import IdeGYMBusyError, IdeGYMHTTPError, IdeGYMNotFoundError

try:
    async with client.with_server(image_tag="registry.example.com/my-env:latest") as server:
        ...
except IdeGYMNotFoundError:
    ...  # the sandbox is gone — start a new one
except IdeGYMBusyError as e:
    ...  # the control plane is rate-limiting — back off and retry
except IdeGYMHTTPError as e:
    print(f"Failed with {e.status_code}: {e.body}")
```

| Exception | Statuses | What it means for a retry |
|-----------|----------|---------------------------|
| `IdeGYMBadRequestError` | 400, 422, other 4xx | The request is wrong; retrying it unchanged will not help |
| `IdeGYMAuthError` | 401, 403 | Credentials are missing, wrong, or insufficient |
| `IdeGYMNotFoundError` | 404, 410 | The client, server, or operation is gone — including a pod the orchestrator can no longer reach |
| `IdeGYMTimeoutError` | 408, 504, client-side timeout | Safe to retry if the operation is idempotent |
| `IdeGYMBusyError` | 429, 503 | Rate-limited or out of capacity; retry with backoff |
| `IdeGYMCancelledError` | 499 | Cancelled before finishing, usually by a disconnect |
| `IdeGYMServerError` | 5xx | The orchestrator or the sandbox failed |

All of them subclass `IdeGYMHTTPError`, which subclasses both `IdeGYMException` and
`RuntimeError`, and the message text is unchanged from before the typed exceptions existed — so
an existing `except RuntimeError` still catches everything it used to.

A `IdeGYMTimeoutError` with `status_code is None` is a client-side timeout: the request never
reached a status. That is the case to distinguish from a 504, where the orchestrator answered.

---

## Complete Example with Rewards

```python
import asyncio
from pathlib import Path
from idegym.client.client import IdeGYMClient
from idegym.image.builder import Image
from idegym.plugins.defaults.image import User, Project


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
