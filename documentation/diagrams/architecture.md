# IdeGYM System Architecture

IdeGYM is a Python framework for creating disposable, scalable development
environments for AI agents. The diagram below shows the high-level component
topology and the main data/control flows.

> **Note on IDE integrations:** PyCharm / IntelliJ are **not** orchestrator
> clients. They run *inside* the disposable server pod as an in-container IDE
> process that exposes an MCP upstream (`:6789`) and an inspection router; the
> in-pod FastAPI server proxies them. The only external clients are the Python
> client library and MCP-speaking AI agents.

```mermaid
flowchart TB
    subgraph clients["Clients"]
        lib["Python Client Library"]
        agent["AI Agents (MCP)"]
    end

    subgraph orch["Orchestrator Service (K8s Deployment)"]
        api["FastAPI + MCP endpoint /mcp"]
        builder["Image Builder (plugin-based)"]
        kaniko["Kaniko build submitter"]
        watcher["Watcher / cleanup loop"]
    end

    pg[("PostgreSQL<br/>clients · servers · jobs · snapshots")]

    subgraph k8s["Kubernetes Cluster"]
        kapi["Kubernetes API"]
        buildjob["Kaniko Build Jobs"]
        subgraph pod["Disposable Server Pod (gVisor sandbox)"]
            sup["Supervisor"]
            srv["FastAPI Server :8000 + MCP gateway"]
            tools["Tools: bash · files · inspect"]
            rewards["Rewards"]
            ide["IDE process (PyCharm / IntelliJ)<br/>+ open-project plugin"]
            mcpup["MCP upstream :6789/mcp"]
        end
    end

    registry[("Docker Registry")]
    otel["OpenTelemetry"]

    %% client access (no IDE here)
    lib -->|REST / forward| api
    agent -->|MCP| api

    %% orchestrator state + control
    api <-->|async SQLAlchemy| pg
    watcher --> pg
    watcher -->|reconcile / cleanup| kapi

    %% image build flow
    api --> builder --> kaniko --> kapi --> buildjob
    buildjob -->|push image| registry

    %% pod lifecycle
    api -->|create deployment| kapi --> pod
    registry -->|pull image| pod

    %% inside the pod
    sup --> srv
    sup --> ide
    srv --> tools
    srv --> rewards
    ide --> mcpup
    srv -->|proxy upstream| mcpup

    %% forwarding + observability
    api -->|HTTP / MCP forward| srv
    srv --> otel
    api --> otel
```

## Overview

- **Clients** — two external access patterns: the Python client library
  (programmatic, with HTTP/MCP forwarding) and MCP for AI agents.
- **Orchestrator** — central FastAPI service (plus MCP endpoint at `/mcp`) that
  manages the full environment lifecycle. It persists state in PostgreSQL,
  builds images via the plugin-based Image Builder + Kaniko, and provisions pods
  on Kubernetes. A background watcher reconciles database state with live
  cluster resources.
- **PostgreSQL** — stores clients, servers, build jobs, and pod snapshots
  (async SQLAlchemy + asyncpg).
- **Image building** — plugin-composable Docker images are compiled to a build
  spec and submitted to Kaniko build jobs in-cluster, which push to the registry.
- **Disposable server pods** — each gVisor-sandboxed pod runs Supervisor +
  FastAPI server (`:8000`) + MCP gateway, exposing tools (bash, files, inspect),
  reward calculation, and — when an IDE plugin is included — an in-container IDE
  process that the server proxies as an MCP upstream (`:6789`).
- **Observability** — both orchestrator and server emit traces/metrics via
  OpenTelemetry.

## Key flows

1. **Build image** — client/orchestrator composes an `Image` (plugins) → build
   spec → Kaniko job → image pushed to the registry.
2. **Provision environment** — orchestrator creates a Kubernetes deployment; the
   pod pulls the image and boots Supervisor → FastAPI server (and any in-pod
   IDE process).
3. **Use environment** — client/agent requests are forwarded by the orchestrator
   through to the server pod's HTTP/MCP endpoints; tools execute inside the
   sandbox.
4. **Cleanup** — the watcher loop reconciles database state with the cluster and
   tears down stale resources.

## Related diagrams

- [Plugin ecosystem](plugins.md) — how plugins hook into image build, server
  routing, and client operations.
- [Server internals](server.md) — how a single in-pod server process is
  assembled.
