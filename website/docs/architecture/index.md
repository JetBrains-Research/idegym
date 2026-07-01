---
title: Architecture
description: Interactive, drill-down architecture of IdeGYM — click any node to open its component page.
slug: /architecture
sidebar_position: 1
---

# Architecture

This is the centerpiece. The diagram below is the **whole system** — and every component
is **clickable**. Click a node to drill into its deep-dive page; each of those pages has
its own diagram that drills further, down to the source on GitHub.

> 💡 **Three levels of drill-down:** this overview (L0) → component pages with their own
> sub-diagrams (L1) → narrative + "view source" links (L2).

## System overview (click any node)

```mermaid
flowchart TB
    subgraph clients["👥 Clients"]
        lib(["🐍 Python Client Library"]):::client
        agent(["🤖 AI Agents · MCP"]):::client
    end

    subgraph orch["🎛️ Orchestrator Service · K8s Deployment"]
        api{{"FastAPI + MCP<br/>/mcp endpoint"}}:::ctrl
        builder[/"Image Builder<br/>plugin-based"/]:::build
        kaniko[/"Kaniko build<br/>submitter"/]:::build
        watcher["Watcher /<br/>cleanup loop"]:::infra
    end

    pg[("🗄️ PostgreSQL<br/>clients · servers · jobs · snapshots")]:::store

    subgraph k8s["☸️ Kubernetes Cluster"]
        kapi["Kubernetes API"]:::infra
        buildjob[/"Kaniko Build Jobs"/]:::build
        subgraph pod["📦 Disposable Server Pod · gVisor sandbox"]
            srv[["FastAPI Server :8000<br/>+ MCP gateway"]]:::pod
            tools("Tools · Rewards"):::tool
            ide("IDE process<br/>PyCharm / IntelliJ"):::pod
        end
    end

    registry[("📦 Docker Registry")]:::store
    otel["📊 Observability<br/>OpenTelemetry"]:::infra

    lib -->|"REST / forward"| api
    agent -->|"MCP"| api
    api <-->|"async SQLAlchemy"| pg
    watcher --> pg
    watcher -->|"reconcile / cleanup"| kapi
    api --> builder --> kaniko --> kapi --> buildjob
    buildjob -->|"push image"| registry
    api -->|"create deployment"| kapi --> pod
    registry -->|"pull image"| pod
    srv --> tools
    srv --> ide
    api -->|"HTTP / MCP forward"| srv
    srv --> otel
    api --> otel

    click lib "/idegym/architecture/client" "Client library"
    click agent "/idegym/architecture/client" "Client & MCP access"
    click api "/idegym/architecture/orchestrator" "Orchestrator"
    click builder "/idegym/architecture/image-builder" "Image builder"
    click kaniko "/idegym/architecture/image-builder" "Kaniko builds"
    click buildjob "/idegym/architecture/image-builder" "Kaniko build jobs"
    click watcher "/idegym/architecture/watcher" "Watcher"
    click pg "/idegym/architecture/orchestrator" "Persistent state"
    click kapi "/idegym/deployment" "Kubernetes & deployment"
    click srv "/idegym/architecture/server" "Server internals"
    click tools "/idegym/architecture/rewards-tools" "Tools & rewards"
    click ide "/idegym/architecture/plugins" "IDE plugins"
    click registry "/idegym/architecture/image-builder" "Registry"
    click otel "/idegym/deployment" "Observability & deployment"

    classDef client fill:#1c7ed6,stroke:#1864ab,color:#fff;
    classDef ctrl fill:#e8590c,stroke:#c04405,color:#fff;
    classDef build fill:#f08c00,stroke:#e67700,color:#fff;
    classDef store fill:#0c8599,stroke:#0b7285,color:#fff;
    classDef pod fill:#7048e8,stroke:#5f3dc4,color:#fff;
    classDef tool fill:#2f9e44,stroke:#2b8a3e,color:#fff;
    classDef infra fill:#495057,stroke:#343a40,color:#fff;
```

<small><em>Underlined nodes are clickable; dashed boxes are groupings. (Adapted from
[`documentation/diagrams/architecture.md`](https://github.com/JetBrains-Research/idegym/blob/main/documentation/diagrams/architecture.md).)</em></small>

## Component map

| Component | What it does | Deep dive |
|---|---|---|
| **Clients** | Python client library + MCP access for agents | [Client →](/architecture/client) |
| **Orchestrator** | FastAPI control plane: lifecycle, state, forwarding | [Orchestrator →](/architecture/orchestrator) |
| **Image Builder / Kaniko** | Compose images from plugins, build in-cluster | [Image builder →](/architecture/image-builder) |
| **Server Pod** | Sandboxed FastAPI environment + MCP gateway | [Server →](/architecture/server) |
| **Plugins** | Image / server / client extension points | [Plugins →](/architecture/plugins) |
| **Watcher** | Cleanup / reconcile loop | [Watcher →](/architecture/watcher) |
| **Rewards & Tools** | What agents do & how they're scored | [Rewards & tools →](/architecture/rewards-tools) |
| **PostgreSQL** | Clients, servers, jobs, snapshots | [Orchestrator →](/architecture/orchestrator) |
| **Registry & Observability** | Image storage, metrics & traces | [Deployment →](/deployment) |

## The key flows

1. **Build image** — orchestrator compiles an `Image` (plugins) → build spec → Kaniko job
   → image pushed to the registry.
2. **Provision** — orchestrator creates a Kubernetes Deployment; the pod pulls the image
   and boots Supervisor → FastAPI server (and any in-pod IDE).
3. **Use** — client/agent requests are forwarded by the orchestrator into the server
   pod's HTTP / MCP endpoints; tools execute inside the sandbox.
4. **Clean up** — the watcher reconciles database state with the cluster and tears down
   stale resources.

---

**New to IdeGYM?** Read the [core concepts](/overview/concepts) first, then the
[end-to-end data flow](/overview/data-flow).
