# Plugin Architecture

IdeGYM's plugin system lets you extend the framework at three distinct integration points, all from a
single package. A plugin can participate in image building (what goes into the container), server
routing (what endpoints the server exposes at runtime), and client operations (what typed methods the
Python client gains). Each integration point is optional — a plugin only needs to implement the parts
it uses.

## Table of Contents

- [Overview](#overview)
- [Integration Points](#integration-points)
- [Image Build Plugins](#image-build-plugins)
- [Server Plugins](#server-plugins)
- [MCP Upstream Convention](#mcp-upstream-convention)
- [Client Operation Plugins](#client-operation-plugins)
- [Plugin Discovery and Configuration](#plugin-discovery-and-configuration)
- [Writing a Full Plugin](#writing-a-full-plugin)
- [Built-in Default Plugins](#built-in-default-plugins)
- [Entry Point Groups Reference](#entry-point-groups-reference)

---

## Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                         Plugin Package                           │
│                                                                  │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────┐  │
│  │  @image_plugin  │  │  @server_plugin  │  │  client ops    │  │
│  │  PluginBase     │  │  get_server_     │  │  (entry point) │  │
│  │  apply/render   │  │  router()        │  │                │  │
│  └────────┬────────┘  └────────┬─────────┘  └───────┬────────┘  │
└───────────┼────────────────────┼────────────────────┼───────────┘
            │                    │                     │
            ▼                    ▼                     ▼
     Dockerfile fragment   FastAPI router        server.pycharm
     written at build time  mounted at startup   .health() etc.
```

The central module is `api/src/idegym/api/plugin.py`. It defines `PluginBase`, `BuildContext`,
and both registries (`@image_plugin`, `@server_plugin`). The `api` package is a lightweight shared
dependency that image builder, server, and client all import without creating cycles.

---

## Integration Points

A class participates in integration points by implementing specific methods or registering with a
decorator:

| Integration Point | How | Where consumed |
|---|---|---|
| Image building | `@image_plugin("name")` + `apply()` / `render()` | `Image.to_spec()` in image builder |
| Server routing | `@server_plugin` + `get_server_router()` | `server/main.py` at startup |
| MCP upstream | `get_mcp_upstream()` on a `PluginBase` subclass | `Image.to_spec()` auto-writes config |
| Client operations | `idegym.plugins.client` entry point | `IdeGYMServer.__init__` |

All methods have no-op defaults, so implementing only one integration point is perfectly valid.

---

## Image Build Plugins

Image build plugins emit Dockerfile fragments. They are registered with `@image_plugin` and subclass
`PluginBase`.

### `PluginBase`

```python
from idegym.api.plugin import BuildContext, PluginBase, image_plugin


@image_plugin("my-plugin")
class MyPlugin(PluginBase):
    # Pydantic fields — validated, serializable to/from YAML
    message: str
    path: str = "/tmp/hello.txt"

    def apply(self, ctx: BuildContext) -> BuildContext:
        """Update the shared build context. Called before render()."""
        return ctx.updated(labels={**ctx.labels, "my.label": self.message})

    def render(self, ctx: BuildContext) -> str:
        """Return a Dockerfile fragment. Called after apply() on ALL plugins."""
        return f"RUN echo {self.message!r} > {self.path}"
```

### `BuildContext`

`BuildContext` is an immutable dataclass threaded through the plugin pipeline. Each plugin's
`apply()` method receives the context output by the previous plugin and returns a new one.

| Field | Type | Default | Description |
|---|---|---|---|
| `base` | `str` | — | Base image reference |
| `current_user` | `str` | `"root"` | Active user; updated by the `user` plugin |
| `home` | `str` | `"/root"` | Active user's home directory |
| `project_root` | `str` | `"/root/work"` | Project root inside the container |
| `request` | `Optional[DownloadRequest]` | `None` | Download request set by the `project` plugin |
| `labels` | `dict[str, str]` | `{}` | OCI image labels accumulated by plugins |
| `context_path` | `str` | `"."` | Docker build context directory |
| `extras` | `dict[str, Any]` | `{}` | Arbitrary inter-plugin state |

**Updating the context:**

```python
# Replace a single field
new_ctx = ctx.updated(current_user="appuser", home="/home/appuser")

# Add a key to extras (for passing state to downstream plugins)
new_ctx = ctx.with_extra("my.plugin.setting", "value")

# Merge multiple extras at once
new_ctx = ctx.with_extras({"my.key": 1, "other.key": 2})

# Read extras in render()
value = ctx.get_extra("my.plugin.setting", default="fallback")
```

### Plugin Pipeline

`Image.to_spec()` processes plugins one at a time: for each plugin, `apply()` runs first to update
the context, then `render()` is called immediately with the updated context to produce the
Dockerfile fragment. The next plugin then receives the context as left by the previous one.

```
BuildContext(base=...)
  → plugin[0].apply(ctx)    → ctx_0
  → plugin[0].render(ctx_0) → fragment_0
  → plugin[1].apply(ctx_0)  → ctx_1
  → plugin[1].render(ctx_1) → fragment_1
  → ...
  → assemble Dockerfile      → ImageBuildSpec
```

> [!NOTE]
> Because `apply()` and `render()` are interleaved, **plugin order matters**. A plugin's `render()`
> sees only the context set by itself and earlier plugins — not by plugins that come after it in the
> list. For example, if the `user` plugin comes after the `project` plugin, `project.render()` will
> see `current_user="root"` (the default), not the user created by the `user` plugin.

### Registering with `@image_plugin`

```python
@image_plugin("my-plugin")   # "my-plugin" is the YAML `type` field
class MyPlugin(PluginBase):
    ...
```

The decorator registers the class in `_PLUGIN_REGISTRY`. The name must be unique across all
installed plugins. Attempting to register the same name twice raises `ValueError`.

**Discovery:** Plugins are loaded via the `idegym.plugins.image` entry point group at import time
(when `idegym.image.builder` is first imported). No manual import is required. Declare the entry
point in your package's `pyproject.toml`:

```toml
[project.entry-points."idegym.plugins.image"]
my-plugin = "my_package.plugins:MyPlugin"
```

---

## Server Plugins

Server plugins mount FastAPI routers into the running IdeGYM server. They are registered with
`@server_plugin`.

```python
from idegym.api.plugin import server_plugin


@server_plugin
class MyPlugin:
    """Expose /my-plugin/* endpoints on the server."""

    @classmethod
    def get_server_router(cls):
        from my_package.router import router  # deferred import avoids circular deps
        return router
```

`get_server_router()` is called at server startup. It should return a FastAPI `APIRouter` or `None`
(to skip registration for this plugin class). Deferred imports inside `get_server_router()` are the
recommended pattern: they avoid loading the router module until the server actually needs it, which
keeps the plugin's `pyproject.toml` dependencies from affecting packages that only use the image
builder.

`@server_plugin` raises `ValueError` if the same class is decorated twice.

### Dependency Injection

Server plugins use FastAPI's native `dependency_overrides` mechanism. Define a stub dependency
function in your router, then let the server wire the real implementation:

```python
# my_package/router.py
from fastapi import APIRouter, Depends

router = APIRouter()


async def _get_my_service() -> MyService:
    """Stub — server overrides this via app.dependency_overrides."""
    raise RuntimeError("my_service not configured")


@router.post("/my-plugin/action")
async def do_action(service: MyService = Depends(_get_my_service)):
    ...
```

The server then registers the real service:

```python
# server/main.py (or your application entry point)
from my_package.router import _get_my_service

app.dependency_overrides[_get_my_service] = lambda: container.my_service()
```

The `lambda: container.my_service()` wrapper is required because FastAPI introspects the callable's
signature via `inspect.signature()`. Dependency-injector `Singleton` providers have a C-level
`__call__` with no inspectable Python signature — wrapping in a lambda gives FastAPI a plain Python
callable with a `()` signature.

### Discovery

The server loads plugins from the `idegym.plugins.server` entry point group at module import time,
filtered by `/etc/idegym/plugins.json` (see [Plugin Discovery and Configuration](#plugin-discovery-and-configuration)):

```toml
[project.entry-points."idegym.plugins.server"]
my-plugin = "my_package.server_plugin:MyPlugin"
```

---

## MCP Upstream Convention

A plugin that runs an [MCP](https://modelcontextprotocol.io) server inside the container can
declare its URL by overriding `get_mcp_upstream()` on the image build plugin class:

```python
@image_plugin("pycharm")
class PyCharm(PluginBase):
    ...

    @classmethod
    def get_mcp_upstream(cls) -> Optional[str]:
        return "http://localhost:6789/mcp"
```

When `Image.to_spec()` encounters a plugin that returns a non-`None` URL, it automatically emits a
Dockerfile instruction:

```dockerfile
# Register MCP upstream: pycharm
RUN set -eux; \
    mkdir -p /etc/idegym/mcp-upstreams.d; \
    printf '%s\n' '{"url":"http://localhost:6789/mcp"}' > /etc/idegym/mcp-upstreams.d/pycharm.json
```

The MCP gateway running in the container reads `/etc/idegym/mcp-upstreams.d/` at startup to
discover upstream MCP servers.

You can also add MCP upstreams explicitly without implementing a plugin, using the `mcp-upstream`
built-in plugin:

```python
from idegym.plugins.defaults.image import MCPUpstream

image = image.with_plugin(MCPUpstream(name="my-service", url="http://localhost:9000/mcp"))
```

```yaml
- type: mcp-upstream
  name: my-service
  url: http://localhost:9000/mcp
```

---

## Client Operation Plugins

Client operation plugins add typed, async operation methods to `IdeGYMServer`. The class is
discovered via the `idegym.plugins.client` entry point group and instantiated with the server's
forwarding context:

```python
from idegym.client.operations.forwarding import ForwardingOperations


class MyPluginClientOperations:
    def __init__(
        self,
        forward: ForwardingOperations,
        server_id,
        client_id,
        polling_config,
    ):
        self._forward = forward
        self._server_id = server_id
        self._client_id = client_id
        self._polling_config = polling_config

    async def do_action(self, payload: str) -> dict:
        from my_package.api import MyRequest

        return await self._forward.forward_request(
            method="POST",
            path="my-plugin/action",
            body=MyRequest(payload=payload),
            server_id=self._server_id,
            client_id=self._client_id,
            polling_config=self._polling_config,
        )
```

Declare the entry point in `pyproject.toml`:

```toml
[project.entry-points."idegym.plugins.client"]
my-plugin = "my_package.client_ops:MyPluginClientOperations"
```

`IdeGYMServer.__init__` iterates the `idegym.plugins.client` group and attaches each loaded class
as an attribute. Hyphens in the entry point name are replaced with underscores so the attribute is
always valid Python syntax — `"my-plugin"` becomes `server.my_plugin`:

```python
server = await client.start_server(...)
result = await server.my_plugin.do_action("hello")
```

Each `IdeGYMServer` instance gets its own ops object, so `server_a.my_plugin` and
`server_b.my_plugin` are independent instances with different `server_id` values.

Failures are isolated per plugin — if one entry point fails to load (e.g., optional dependency not
installed), the other plugins still load and a warning is emitted.

### `server.forward()` — escape hatch

For endpoints not covered by a typed ops class, use `IdeGYMServer.forward()`:

```python
response = await server.forward(
    method="POST",
    path="my-plugin/action",
    body=MyRequest(payload="hello"),
)
```

`forward()` delegates to the same `ForwardingOperations` used by all other operations. It accepts
any Pydantic model as `body` and returns the parsed JSON response as a `dict`.

---

## Plugin Discovery and Configuration

### Entry Point Groups

| Group | Loaded by | When |
|---|---|---|
| `idegym.plugins.image` | `image-builder` (`builder.py`) | At import time — ensures the registry is populated before YAML deserialization |
| `idegym.plugins.server` | `server/main.py` | At module load — filtered by `plugins.json` |
| `idegym.plugins.client` | `client/server.py` (`IdeGYMServer.__init__`) | Per `IdeGYMServer` instance — deferred import picks up any runtime patches |

### `/etc/idegym/plugins.json`

The `IdeGYMServer` image build plugin writes this file into the container at build time. It controls
which server and client plugins are enabled at runtime:

```json
{"server": ["tools", "rewards", "pycharm"]}
```

The list contains entry point names from the `idegym.plugins.server` group. At server startup:

- If the file exists → load only the listed plugins
- If the file is absent (development) → load all installed plugins

The `IdeGYMServer` plugin always includes `tools` and `rewards`. Additional plugins can be added
by other `@image_plugin` classes that call `ctx.with_extra("idegym.enabled_server_plugins", [...])`.
The `PyCharm` plugin does this automatically when added to an image:

```python
image = (
    image
    .with_plugin(PyCharm(version="2024.3.1", user="appuser"))   # adds "pycharm" to the list
    .with_plugin(IdeGYMServer.from_local(root=from_root()))      # writes plugins.json
)
```

The directory `/etc/idegym/` and the file are owned by the container user (`appuser`) so that
subsequent `run_commands()` calls can overwrite `plugins.json` if needed.

---

## Writing a Full Plugin

This example shows a plugin that participates in all integration points.

### 1. Define the image build plugin

```python
# my_package/image_plugin.py
from typing import Optional
from idegym.api.plugin import BuildContext, PluginBase, image_plugin


@image_plugin("my-plugin")
class MyPlugin(PluginBase):
    port: int = 8090

    def apply(self, ctx: BuildContext) -> BuildContext:
        # Signal to IdeGYMServer (if in the pipeline) to enable our server plugin
        existing = list(ctx.get_extra("idegym.enabled_server_plugins", []))
        return ctx.with_extra("idegym.enabled_server_plugins", existing + ["my-plugin"])

    def render(self, ctx: BuildContext) -> str:
        return f"RUN pip install my-service && my-service --install"

    @classmethod
    def get_mcp_upstream(cls) -> Optional[str]:
        return f"http://localhost:8090/mcp"
```

### 2. Define the server plugin

```python
# my_package/server_plugin.py
from idegym.api.plugin import server_plugin


@server_plugin
class MyServerPlugin:
    @classmethod
    def get_server_router(cls):
        from my_package.router import router
        return router
```

```python
# my_package/router.py
from fastapi import APIRouter, Depends

router = APIRouter()


async def _get_my_service():
    raise RuntimeError("not configured")


@router.get("/my-plugin/status")
async def status():
    return {"ok": True}
```

### 3. Define the client operations class

```python
# my_package/client_ops.py
class MyPluginClientOperations:
    def __init__(self, forward, server_id, client_id, polling_config):
        self._forward = forward
        self._server_id = server_id
        self._client_id = client_id
        self._polling_config = polling_config

    async def status(self) -> dict:
        return await self._forward.forward_request(
            method="GET",
            path="my-plugin/status",
            server_id=self._server_id,
            client_id=self._client_id,
            polling_config=self._polling_config,
        )
```

### 4. Declare entry points in `pyproject.toml`

```toml
[project.entry-points."idegym.plugins.image"]
my-plugin = "my_package.image_plugin:MyPlugin"

[project.entry-points."idegym.plugins.server"]
my-plugin = "my_package.server_plugin:MyServerPlugin"

[project.entry-points."idegym.plugins.client"]
my-plugin = "my_package.client_ops:MyPluginClientOperations"
```

### 5. Use the plugin

```python
from idegym.image.builder import Image
from my_package.image_plugin import MyPlugin

image = (
    Image.from_base("ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest")
    .with_plugin(User(username="appuser", uid=1000, gid=1000))
    .with_plugin(MyPlugin(port=8090))
    .with_plugin(IdeGYMServer.from_local(root=from_root()))
)

# After building and starting the server:
status = await server.my_plugin.status()   # → {"ok": True}
```

---

## Built-in Default Plugins

### `idegym-plugin-defaults` (always installed)

Ships with the IdeGYM workspace. Source: `plugins/defaults/src/idegym/plugins/defaults/`.

**`image.py`** — image build plugins:

| Type name | Class | Description |
|---|---|---|
| `base-system` | `BaseSystem` | Installs system packages via `apt-get` |
| `user` | `User` | Creates a Linux user and group |
| `permissions` | `Permissions` | Sets file ownership and mode |
| `mcp-upstream` | `MCPUpstream` | Explicitly declares an MCP upstream URL |
| `project` | `Project` | Loads a project (git, local copy, archive, clone) |
| `idegym-server` | `IdeGYMServer` | Installs the IdeGYM server runtime |

**`server.py`** — server plugins (no image build component):

| Entry point name | Class | Description |
|---|---|---|
| `tools` | `ToolsPlugin` | Mounts the built-in tools router (`/api/tools/*`) |
| `rewards` | `RewardsPlugin` | Mounts the built-in rewards router (`/api/rewards/*`) |

### `idegym-plugin-pycharm` (optional, separate package)

Ships in `plugins/pycharm/`. Install separately to use the PyCharm integration.

| Integration point | Entry point group | Class |
|---|---|---|
| Image build | `idegym.plugins.image` | `PyCharm` — installs PyCharm IDE; uses bundled JBR for Java |
| Server routing | `idegym.plugins.server` | `PyCharmPlugin` — mounts `GET /api/pycharm/health` |
| Client operations | `idegym.plugins.client` | `PycharmClientOperations` — exposes `server.pycharm.health()` |

---

## Entry Point Groups Reference

```
idegym.plugins.image
    ├─ base-system   → idegym.plugins.defaults.image:BaseSystem
    ├─ user          → idegym.plugins.defaults.image:User
    ├─ permissions   → idegym.plugins.defaults.image:Permissions
    ├─ mcp-upstream  → idegym.plugins.defaults.image:MCPUpstream
    ├─ project       → idegym.plugins.defaults.image:Project
    ├─ idegym-server → idegym.plugins.defaults.image:IdeGYMServer
    └─ pycharm       → idegym.plugins.pycharm.image:PyCharm

idegym.plugins.server
    ├─ tools         → idegym.plugins.defaults.server:ToolsPlugin
    ├─ rewards       → idegym.plugins.defaults.server:RewardsPlugin
    └─ pycharm       → idegym.plugins.pycharm.server:PyCharmPlugin

idegym.plugins.client
    └─ pycharm       → idegym.plugins.pycharm.client:PycharmClientOperations
```
