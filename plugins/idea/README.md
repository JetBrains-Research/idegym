# IdeGYM IDEA Plugin

Adds IntelliJ IDEA Community to an IdeGYM image together with the JetBrains MCP
server plugin, making the IDE fully controllable by an AI agent via the Model
Context Protocol.

## What it does

When added to an IdeGYM image pipeline, the `Idea` plugin:

1. Installs system dependencies (socat, X11/font libs for the JVM).
2. Downloads and verifies IntelliJ IDEA Community from JetBrains CDN (sha256-checked).
3. Adds headless and startup-suppression flags to `idea64.vmoptions` so the IDE
   starts without a display server and without first-run wizards.
4. Installs the [JetBrains MCP server plugin](https://plugins.jetbrains.com/plugin/26071-mcp-server/versions)
   into the bundled plugins directory.
5. Pre-writes IDE settings to `/tmp/ide-config` (MCP auto-start, EUA acceptance,
   project trust, suppressed first-run dialogs).
6. Optionally installs the **open-project** plugin and registers a supervisord
   service that opens the project directory on container start.

At runtime supervisord calls `start-idea.sh`, which:

1. Exports `JAVA_TOOL_OPTIONS="-Djava.awt.headless=true"` so the JVM reads the flag
   before any application code runs — IDEA Community supports true headless mode and
   does not require Xvfb.
2. Launches IDEA with `-Didea.config.path=/tmp/ide-config` and
   `-Didea.system.path=/tmp/ide-system` so all paths are predictable inside the
   container.
3. Invokes the **open-project** plugin via the `open` `AppStarter` command, which
   opens `IDEGYM_PROJECT_ROOT`.
4. Waits until the MCP server binds on `127.0.0.1:64342`.
5. Starts a **socat** bridge: `TCP-LISTEN:64343 → 127.0.0.1:64342`, exposing the
   MCP server on all interfaces at port `64343`.

## Quick start

```python
from idegym.image.builder import Image
from idegym.plugins.defaults.image import Project
from idegym.plugins.idea.image import Idea

image = (
    Image.from_base("registry.example.com/server-debian-bookworm:latest")
    .with_plugin(Project.from_git("https://github.com/owner/repo.git", ref="main"))
    .with_plugin(Idea(version="2025.3"))
)
spec = image.to_spec()
```

## Accessing the MCP server

### Via IdeGYM (Kubernetes)

The `Idea` plugin calls `get_mcp_upstream()` which causes `image.to_spec()` to
automatically write `/etc/idegym/mcp-upstreams.d/idea.json`. The IdeGYM server
discovers this file and proxies MCP traffic, so the AI agent reaches IDEA's MCP
server through the standard IdeGYM MCP endpoint without any extra configuration.

From Python:

```python
from idegym.client import IdeGYMClient

async with IdeGYMClient(...) as client:
    server = await client.create_server(spec)
    result = await server.idea.health()
    print(result["mcp_url"])  # http://localhost:64342
```

### Standalone (Docker)

The socat bridge makes port `64343` reachable on all interfaces, so you can run
the image directly and connect any MCP client to it without IdeGYM:

```bash
docker run --rm -p 64343:64343 <image>
# MCP endpoint: http://localhost:64343/mcp
```

## Client operations

`IdeGYMServer` attaches an `idea` attribute (an `IdeaClientOperations` instance)
when the IDEA client entry point is discovered:

| Method | HTTP | Description |
|--------|------|-------------|
| `await server.idea.health()` | `GET /idea/health` | Returns `{"mcp_url": "http://localhost:64342"}` confirming the MCP server is reachable |

## Configuration

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `version` | `str` | `"2025.3"` | IDEA version (`YYYY.N` or `YYYY.N.N`) |
| `mcp_update_id` | `Optional[str]` | `"882474"` | Marketplace update ID for the MCP plugin (see [versions](https://plugins.jetbrains.com/plugin/26071-mcp-server/versions)); `None` skips installation |
| `open_project` | `bool` | `True` | Install the open-project plugin and supervisord service when a `Project` plugin is in the pipeline |
| `user` | `Optional[str]` | `ctx.current_user` | Linux user to switch back to after installation |

## Ports

| Port | Protocol | Description |
|------|----------|-------------|
| `64342` | TCP | MCP server (loopback only, bound by the JetBrains plugin) |
| `64343` | TCP | socat bridge — same MCP server, reachable on all interfaces |

## Architecture notes

**Why no Xvfb?** IntelliJ IDEA Community supports `java.awt.headless=true` natively.
The PyCharm plugin requires Xvfb because PyCharm CE does not have this support.
Running headless eliminates an entire process, reduces startup time, and lowers memory
usage.

**Why `JAVA_TOOL_OPTIONS`?** Setting `-Djava.awt.headless=true` only via JVM CLI
arguments can be overridden by `idea.sh`'s own argument processing. Exporting it
through `JAVA_TOOL_OPTIONS` ensures the JVM picks it up unconditionally before any
application code runs.

**Why a fixed config path?** Without `-Didea.config.path`, IDEA resolves its config
directory through XDG conventions using `$HOME`. In containers `$HOME` may be unset
or point to an unexpected location, especially when running as a non-root user.
Writing all settings to `/tmp/ide-config` at build time and always passing the flag
explicitly guarantees the IDE picks up the correct settings.

**Why socat?** The JetBrains MCP plugin binds to `127.0.0.1:64342` (loopback only)
for security. socat forwards `0.0.0.0:64343 → 127.0.0.1:64342` so the MCP server
is reachable from outside the container without patching the plugin.

## Rebuilding the open-project plugin

The pre-built `project-opener/project-opener.zip` is checked into the repository to
avoid a Gradle stage in the Docker build. To rebuild it after changes to the Kotlin
source:

```bash
cd plugins/idea/project-opener
./gradlew buildPlugin
cp build/distributions/open-project-*.zip project-opener.zip
```

Requires JDK 17+ and Gradle (or use the wrapper). The plugin targets build series
252+ (IDEA 2025.2+).
