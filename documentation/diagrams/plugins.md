# Plugin Ecosystem

A single plugin package can hook into three distinct integration points. Each is
optional, discovered via its own entry-point group, and consumed by a different
part of the system. See [Plugin Architecture](../plugins.md) for the full
authoring guide.

```mermaid
flowchart LR
    subgraph pkg["Plugin Package (e.g. idegym-plugin-pycharm)"]
        imgp["@image_plugin<br/>PluginBase: apply() / render()<br/>get_mcp_upstream()"]
        srvp["@server_plugin<br/>get_server_router()"]
        cliops["Client Ops class<br/>(typed async methods)"]
    end

    subgraph eps["Entry-point groups"]
        ep_img["idegym.plugins.image"]
        ep_srv["idegym.plugins.server"]
        ep_cli["idegym.plugins.client"]
    end

    imgp --- ep_img
    srvp --- ep_srv
    cliops --- ep_cli

    subgraph consumers["Consumed by"]
        toSpec["Image.to_spec()<br/>(image-builder)"]
        main["server/main.py<br/>(at startup)"]
        idegymsrv["IdeGYMServer.__init__<br/>(client)"]
    end

    ep_img -->|load at import| toSpec
    ep_srv -->|filtered by plugins.json| main
    ep_cli -->|per instance| idegymsrv

    %% outputs
    toSpec --> frag["Dockerfile fragment +<br/>/etc/idegym/mcp-upstreams.d/&lt;name&gt;.json"]
    main --> router["APIRouter mounted at<br/>/api/&lt;plugin&gt;/*"]
    idegymsrv --> attr["server.&lt;plugin&gt;.&lt;method&gt;()<br/>e.g. server.pycharm.inspect()"]

    cfg["/etc/idegym/plugins.json<br/>{server: [tools, rewards, pycharm]}"] -.->|enables| main

    subgraph builtins["Built-in plugins"]
        d1["defaults: base-system · user · permissions<br/>mcp-upstream · project · idegym-server<br/>tools · rewards"]
        d2["pycharm: PyCharm · PyCharmPlugin · PycharmClientOperations"]
        d3["idea: Idea · IdeaPlugin · IdeaClientOperations"]
    end
    builtins -.-> pkg
```

## Integration points

| Integration point | How | Where consumed | Output |
|---|---|---|---|
| Image building | `@image_plugin("name")` + `apply()` / `render()` | `Image.to_spec()` (image-builder) | Dockerfile fragment |
| MCP upstream | `get_mcp_upstream()` on a `PluginBase` subclass | `Image.to_spec()` (auto-writes config) | `/etc/idegym/mcp-upstreams.d/<name>.json` |
| Server routing | `@server_plugin` + `get_server_router()` | `server/main.py` at startup | `APIRouter` mounted at `/api/<plugin>/*` |
| Client operations | `idegym.plugins.client` entry point | `IdeGYMServer.__init__` | `server.<plugin>.<method>()` |

All methods have no-op defaults, so a plugin only needs to implement the parts
it uses.

## Entry-point groups

| Group | Loaded by | When |
|---|---|---|
| `idegym.plugins.image` | `image-builder` (`builder.py`) | At import time — registry populated before YAML deserialization |
| `idegym.plugins.server` | `server/main.py` | At module load — filtered by `/etc/idegym/plugins.json` |
| `idegym.plugins.client` | `client/server.py` (`IdeGYMServer.__init__`) | Per `IdeGYMServer` instance |

`/etc/idegym/plugins.json` (written by the `IdeGYMServer` image plugin at build
time) controls which server plugins are enabled at runtime. If the file is
absent (development), all installed plugins are loaded.

## Related diagrams

- [System architecture](architecture.md)
- [Server internals](server.md)
