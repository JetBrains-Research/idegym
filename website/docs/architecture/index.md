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

## System overview (click any node)

```mermaid
flowchart TB
    subgraph clients["👥 Clients"]
        lib(["🐍 Python Client Library"]):::client
        agent(["🤖 AI Agents · MCP"]):::client
    end

    subgraph orch["🎛️ Orchestrator Service · K8s Deployment"]
        api{{"FastAPI + MCP"}}:::ctrl
        builder[/"Image Builder"/]:::build
        kaniko[/"Kaniko"/]:::build
        watcher["Watcher"]:::infra
    end

    pg[("🗄️ PostgreSQL")]:::store

    subgraph k8s["☸️ Kubernetes Cluster"]
        kapi["Kubernetes API"]:::infra
        buildjob[/"Kaniko Build Jobs"/]:::build
        subgraph pod["📦 Disposable Server Pod · gVisor sandbox"]
            srv[["Server + MCP"]]:::pod
            tools("Tools · Rewards"):::tool
            ide("IDE · PyCharm / IntelliJ"):::pod
        end
    end

    registry[("📦 Registry")]:::store
    otel["📊 Observability"]:::infra

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

    classDef client fill:#2563eb,stroke:#1d4ed8,color:#fff;
    classDef ctrl fill:#6b57ff,stroke:#5b4bd2,color:#fff;
    classDef build fill:#c026d3,stroke:#a21caf,color:#fff;
    classDef store fill:#0891b2,stroke:#0e7490,color:#fff;
    classDef pod fill:#4f46e5,stroke:#4338ca,color:#fff;
    classDef tool fill:#e23b3b,stroke:#c02626,color:#fff;
    classDef infra fill:#475569,stroke:#334155,color:#fff;
```

<small><em>Underlined nodes are clickable; dashed boxes are groupings. (Adapted from
[`website/docs/reference/diagrams/architecture.md`](https://github.com/JetBrains-Research/idegym/blob/main/website/docs/reference/diagrams/architecture.md).)</em></small>

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
