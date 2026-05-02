# IdeGYM PyCharm Plugin

Adds PyCharm Community (or Professional) to an IdeGYM image together with the
JetBrains MCP server plugin, making the IDE fully controllable by an AI agent
via the Model Context Protocol.

## What it does

When added to an IdeGYM image pipeline, the `PyCharm` plugin:

1. Installs system dependencies (Xvfb, socat, X11 libs, fonts).
2. Downloads and verifies PyCharm CE/Pro from JetBrains CDN (sha256-checked).
3. Installs the [JetBrains MCP server plugin](https://plugins.jetbrains.com/plugin/26071-mcp-server/versions)
   into the bundled plugins directory.
4. Pre-writes IDE settings to `/tmp/ide-config` (MCP auto-start, EUA acceptance,
   project trust, suppressed first-run dialogs).
5. Optionally installs the **open-project** plugin and registers a supervisord
   service that opens the project directory on container start.

At runtime supervisord calls `start-pycharm.sh`, which:

1. Starts **Xvfb** on `:99` — PyCharm CE requires a display; `java.awt.headless=true`
   is not supported.
2. Launches PyCharm with `-Didea.config.path=/tmp/ide-config` and
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
from idegym.plugins.pycharm.image import PyCharm

image = (
    Image.from_base("registry.example.com/server-debian-bookworm:latest")
    .with_plugin(Project.from_git("https://github.com/owner/repo.git", ref="main"))
    .with_plugin(PyCharm(version="2025.3"))
)
spec = image.to_spec()
```

## Accessing the MCP server

### Via IdeGYM (Kubernetes)

The `PyCharm` plugin calls `get_mcp_upstream()` which causes `image.to_spec()` to
automatically write `/etc/idegym/mcp-upstreams.d/pycharm.json`. The IdeGYM server
discovers this file and proxies MCP traffic, so the AI agent reaches PyCharm's MCP
server through the standard IdeGYM MCP endpoint without any extra configuration.

### Standalone (Docker)

The socat bridge makes port `64343` reachable on all interfaces, so you can run
the image directly and connect any MCP client to it without IdeGYM:

```bash
docker run --rm -p 64343:64343 <image>
# MCP endpoint: http://localhost:64343/mcp
```

## Client operations

`IdeGYMServer` attaches a `pycharm` attribute (a `PycharmClientOperations` instance)
when the PyCharm client entry point is discovered:

| Method | HTTP | Description |
|--------|------|-------------|
| `await server.pycharm.inspect(project_path, profile_path, output_dir, ...)` | `POST /pycharm/inspect` | Run `inspect.sh` on the server and return `InspectResponse(output_dir, exit_code)` |

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

**Note:** PyCharm CE requires a display. Before calling `inspect()`, start Xvfb in the
container (e.g. `Xvfb :99 -screen 0 1024x768x24 &` via `server.execute_bash()`).
The `DISPLAY=:99` environment variable is pre-set in the image. The server plugin
automatically runs a background `xdotool` loop to dismiss the Data Sharing modal
that PyCharm CE 2024.x shows ~30-50 s after startup despite all suppression flags.

### Reading inspection results

Inspection result files (XML or JSON) are written to `output_dir` inside the container.
Read them with a follow-up `execute_bash` call:

```python
result = await server.pycharm.inspect(
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
| `version` | `str` | `"2025.3"` | PyCharm version (`YYYY.N` or `YYYY.N.N`) |
| `edition` | `str` | `"community"` | `"community"` or `"professional"` |
| `mcp_update_id` | `Optional[str]` | `"882474"` | Marketplace update ID for the MCP plugin (see [versions](https://plugins.jetbrains.com/plugin/26071-mcp-server/versions)); `None` skips installation |
| `open_project` | `bool` | `True` | Install the open-project plugin and supervisord service when a `Project` plugin is in the pipeline |
| `user` | `Optional[str]` | `ctx.current_user` | Linux user to switch back to after installation |

## Ports

| Port | Protocol | Description |
|------|----------|-------------|
| `64342` | TCP | MCP server (loopback only, bound by the JetBrains plugin) |
| `64343` | TCP | socat bridge — same MCP server, reachable on all interfaces |

## Architecture notes

**Why Xvfb?** PyCharm CE does not support `java.awt.headless=true`. A virtual
framebuffer is mandatory even when no UI interaction is expected. The IDEA plugin
does not have this limitation.

**Why a fixed config path?** Without `-Didea.config.path`, PyCharm resolves its
config directory through XDG conventions using `$HOME`. In containers `$HOME` may be
unset or point to an unexpected location, especially when running as a non-root user.
Writing all settings to `/tmp/ide-config` at build time and always passing the flag
explicitly guarantees the IDE picks up the correct settings.

**Why socat?** The JetBrains MCP plugin binds to `127.0.0.1:64342` (loopback only)
for security. socat forwards `0.0.0.0:64343 → 127.0.0.1:64342` so the MCP server
is reachable from outside the container without patching the plugin.

**Why Marketplace/SettingsSync disabled?** PyCharm CE shows blocking modal dialogs
on the Xvfb display when these plugins are active at first start. They are listed in
`/tmp/ide-config/disabled_plugins.txt` to prevent this.

## Rebuilding the open-project plugin

The pre-built `project-opener/project-opener.zip` is checked into the repository to
avoid a Gradle stage in the Docker build. To rebuild it after changes to the Kotlin
source:

```bash
cd plugins/pycharm/project-opener
./gradlew buildPlugin
cp build/distributions/open-project-*.zip project-opener.zip
```

Requires JDK 17+ and Gradle (or use the wrapper). The plugin targets build series
252+ (PyCharm 2025.2+).
