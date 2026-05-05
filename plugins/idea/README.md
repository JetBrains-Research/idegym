# IdeGYM IDEA Plugin

Adds IntelliJ IDEA to an IdeGYM image together with the JetBrains MCP
server plugin, making the IDE fully controllable by an AI agent via the Model
Context Protocol.

**Requires IDEA 2026.1.1 or newer. Older versions are not supported.**

Starting with 2026.1.1, the MCP server plugin is bundled in IDEA.

## What it does

When added to an IdeGYM image pipeline, the `Idea` plugin:

1. Installs system dependencies (socat, X11/font libs for the JVM).
2. Downloads and verifies IntelliJ IDEA from JetBrains CDN (sha256-checked).
3. Adds headless and startup-suppression flags to `idea64.vmoptions` so the IDE
   starts without a display server and without first-run wizards.
4. Pre-writes IDE settings to `/tmp/ide-config` (MCP auto-start, EUA acceptance,
   project trust, suppressed first-run dialogs).
5. Optionally installs the **open-project** plugin and registers a supervisord
   service that opens the project directory on container start.

At runtime supervisord calls `start-idea.sh`, which:

1. Exports `JAVA_TOOL_OPTIONS="-Djava.awt.headless=true"` so the JVM reads the flag
   before any application code runs — IDEA supports true headless mode and
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
    .with_plugin(Idea(version="2026.1.1"))
)
spec = image.to_spec()
```

## Accessing the MCP server

### Via IdeGYM (Kubernetes)

The `Idea` plugin calls `get_mcp_upstream()` which causes `image.to_spec()` to
automatically write `/etc/idegym/mcp-upstreams.d/idea.json`. The IdeGYM server
discovers this file and proxies MCP traffic, so the AI agent reaches IDEA's MCP
server through the standard IdeGYM MCP endpoint without any extra configuration.

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
| `await server.idea.inspect(project_path, profile_path, output_dir, ...)` | `POST /idea/inspect` | Run `inspect.sh` on the server and return `InspectResponse(output_dir, exit_code)` |

### `inspect()` parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project_path` | `str` | — | Absolute path to the project inside the container |
| `profile_path` | `str` | — | Absolute path to an inspection profile XML file |
| `output_dir` | `str` | — | Directory where result files will be written |
| `changes_only` | `bool` | `False` | Only inspect locally changed files (`-changes`) |
| `directory` | `Optional[str]` | `None` | Limit scope to a subdirectory (`-d`) |
| `format` | `str` | `"xml"` | Output format: `"xml"` or `"json"` |
| `verbosity` | `int` | `0` | Verbosity level 0–2 (`-v0`/`-v1`/`-v2`) |
| `timeout` | `float` | `600.0` | Maximum seconds for `inspect.sh` to run |
| `request_timeout` | `Optional[int]` | `None` | HTTP request timeout override (seconds) |

**Note:** IDEA supports true headless mode (`java.awt.headless=true`). No
Xvfb is needed; `inspect.sh` works without a display server.

### Reading inspection results

Inspection result files (XML or JSON) are written to `output_dir` inside the container.
Read them with a follow-up `execute_bash` call:

```python
result = await server.idea.inspect(
    project_path="/root/work",
    profile_path="/root/work/.idea/inspectionProfiles/Default.xml",
    output_dir="/tmp/inspect-out",
)
assert result.exit_code == 0

listing = await server.execute_bash("ls /tmp/inspect-out/")
xml_output = await server.execute_bash("cat /tmp/inspect-out/*.xml")
```

## Configuration

| Attribute | Type | Default | Description |
|-----------|------|---------|-------------|
| `version` | `str` | `"2026.1.1"` | IDEA version (`YYYY.N` or `YYYY.N.N`); must be 2026.1.1+, older versions not supported |
| `open_project` | `bool` | `True` | Install the open-project plugin and supervisord service when a `Project` plugin is in the pipeline |
| `user` | `Optional[str]` | `ctx.current_user` | Linux user to switch back to after installation |

### MCP plugin

The [JetBrains MCP server plugin](https://plugins.jetbrains.com/plugin/26071-mcp-server/versions)
is bundled in IDEA 2026.1.1+. When `open_project=True` and a `Project` plugin is present,
the plugin automatically enables MCP auto-start by writing
`/tmp/ide-config/options/mcpServer.xml`.

## Ports

| Port | Protocol | Description |
|------|----------|-------------|
| `64342` | TCP | MCP server (loopback only, bound by the JetBrains plugin) |
| `64343` | TCP | socat bridge — same MCP server, reachable on all interfaces |

## Architecture notes

**Why no Xvfb?** IntelliJ IDEA supports `java.awt.headless=true` natively.
The PyCharm plugin requires Xvfb because PyCharm does not have this support.
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
261+ (IDEA 2026.1+).
