---
title: Client
description: The async Python client — IdeGYMClient + IdeGYMServer, typed operations, plugin ops, and the forward() escape hatch.
---

# Client

The `idegym-client` package is the **programmatic front door**. It handles
authentication, client registration, server lifecycle, and every operation on a running
environment — all async. Agents that prefer tools over code can use the orchestrator's
[MCP interface](/reference/mcp) instead; both reach the same handlers.

## Client surface (click a node for source)

```mermaid
flowchart TB
    client(["🐍 IdeGYMClient<br/>async context manager"]):::client
    server[["📦 IdeGYMServer<br/>one per environment"]]:::pod

    subgraph ops["Typed operations"]
        clients_op("clients<br/>register / heartbeat"):::tool
        servers_op("servers<br/>start / stop / finish / restart"):::tool
        jobs_op("jobs<br/>build_and_push_images"):::tool
        tools_op("tools<br/>execute_bash"):::tool
        files_op("files<br/>create / edit / patch"):::tool
        rewards_op("rewards<br/>compilation / setup / test"):::tool
        fwd("forwarding<br/>forward()"):::ctrl
    end

    plugin_ops{{"Plugin ops · per instance<br/>server.pycharm · server.idea"}}:::build

    client --> servers_op
    client --> jobs_op
    client -->|"with_server()"| server
    server --> tools_op
    server --> files_op
    server --> rewards_op
    server --> fwd
    server -.->|"idegym.plugins.client"| plugin_ops

    click client "https://github.com/JetBrains-Research/idegym/blob/main/client/src/idegym/client/client.py" "IdeGYMClient source"
    click server "https://github.com/JetBrains-Research/idegym/blob/main/client/src/idegym/client/server.py" "IdeGYMServer source"
    click fwd "https://github.com/JetBrains-Research/idegym/blob/main/client/src/idegym/client/operations/forwarding.py" "forwarding source"
    click servers_op "https://github.com/JetBrains-Research/idegym/blob/main/client/src/idegym/client/operations/servers.py" "servers ops source"
    click jobs_op "https://github.com/JetBrains-Research/idegym/blob/main/client/src/idegym/client/operations/jobs.py" "jobs ops source"
    click tools_op "https://github.com/JetBrains-Research/idegym/blob/main/client/src/idegym/client/operations/tools.py" "tools ops source"
    click rewards_op "https://github.com/JetBrains-Research/idegym/blob/main/client/src/idegym/client/operations/rewards.py" "rewards ops source"

    classDef client fill:#1c7ed6,stroke:#1864ab,color:#fff;
    classDef pod fill:#7048e8,stroke:#5f3dc4,color:#fff;
    classDef tool fill:#2f9e44,stroke:#2b8a3e,color:#fff;
    classDef ctrl fill:#e8590c,stroke:#c04405,color:#fff;
    classDef build fill:#f08c00,stroke:#e67700,color:#fff;
```

## `IdeGYMClient`

The entry point, used as an **async context manager**: entering registers the client with
the orchestrator; exiting deregisters it and stops its servers.

```python
async with IdeGYMClient(
    orchestrator_url="https://idegym.example.com",
    name="my-training-run",
    namespace="idegym",
    auth=BasicAuth(username="admin", password="..."),
) as client:
    async with client.with_server(image_tag="...", server_name="s1") as server:
        result = await server.execute_bash("echo hello")
```

Key methods: `with_server(...)` (start + auto-cleanup), explicit `start_server` /
`stop_server` / `finish_server`, `jobs.build_and_push_images(...)`, and `health_check()`.
The `reuse_strategy` (`NONE` / `RESTART` / `RESET`) and `close_action` (`FINISH` / `STOP`)
control reuse — central to fast RL episodes.

## `IdeGYMServer`

Returned by `with_server()` / `start_server()`. It exposes the environment's operations:

- **Tools** — `execute_bash`, `create_file`, `edit_file`, `patch_file`, `reset_project`.
- **Rewards** — `compilation_reward`, `setup_reward`, `test_reward`.
- **Lifecycle** — `restart_server`, `list_capabilities` (the runtime plugin list).
- **OpenEnv** — `openenv_url` for WebSocket/OpenEnv-compatible access.

## Plugin operations & `forward()`

When `IdeGYMServer` is constructed it loads the `idegym.plugins.client` entry points and
attaches each as an attribute (hyphens → underscores), so an installed PyCharm plugin
gives you `server.pycharm.inspect(...)`. Each server instance gets its own ops objects.

For endpoints without a typed wrapper, the **escape hatch** is `forward()`:

```python
from idegym.api.tools.bash import BashCommandRequest

response = await server.forward(method="POST", path="tools/bash",
                                body=BashCommandRequest(command="echo hi", timeout=30.0))
```

It delegates to the same `ForwardingOperations` every typed method uses, accepts any
Pydantic body, and returns the parsed JSON. See [plugins](/architecture/plugins) for how
the three plugin hook points line up.

## View source

- `IdeGYMClient` → [`client/src/idegym/client/client.py`](https://github.com/JetBrains-Research/idegym/blob/main/client/src/idegym/client/client.py)
- `IdeGYMServer` + plugin ops loop → [`client/src/idegym/client/server.py`](https://github.com/JetBrains-Research/idegym/blob/main/client/src/idegym/client/server.py)
- Operations → [`client/src/idegym/client/operations/`](https://github.com/JetBrains-Research/idegym/tree/main/client/src/idegym/client/operations)
- Full reference → [Client Library docs](/reference/client)
