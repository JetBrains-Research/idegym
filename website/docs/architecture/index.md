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
        lib(["<b>🐍 Python Client Library</b>"]):::client
        agent(["<b>🤖 AI Agents · MCP</b>"]):::client
    end

    subgraph orch["🎛️ Orchestrator Service · K8s Deployment"]
        api{{"<b>FastAPI + MCP</b>"}}:::ctrl
        builder[/"<b>Image Builder</b>"/]:::build
        kaniko[/"<b>Kaniko</b>"/]:::build
        watcher["<b>Watcher</b>"]:::infra
    end

    pg[("<b>🗄️ PostgreSQL</b>")]:::store

    subgraph k8s["☸️ Kubernetes Cluster"]
        kapi["<b>Kubernetes API</b>"]:::infra
        buildjob[/"<b>Kaniko Build Jobs</b>"/]:::build
        subgraph pod["📦 Disposable Server Pod · gVisor sandbox"]
            srv[["<b>Server + MCP</b>"]]:::pod
            tools("<b>Tools · Rewards</b>"):::tool
            ide[["<b>IDE · PyCharm / IntelliJ</b>"]]:::pod
        end
    end

    registry[("<b>📦 Registry</b>")]:::store
    otel["<b>📊 Observability</b>"]:::infra

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

    click lib "/idegym/architecture/client" "Dive into the Python client library."
    click agent "/idegym/architecture/client" "See how AI agents connect over MCP."
    click api "/idegym/architecture/orchestrator" "Dive into the orchestrator — the FastAPI control plane."
    click builder "/idegym/architecture/image-builder" "See how images are composed from plugins."
    click kaniko "/idegym/architecture/image-builder" "See how images are built in-cluster with Kaniko."
    click buildjob "/idegym/architecture/image-builder" "See how Kaniko build jobs run in the cluster."
    click watcher "/idegym/architecture/watcher" "See how the watcher reclaims stale resources."
    click pg "/idegym/architecture/orchestrator" "See how the orchestrator persists all its state."
    click kapi "/idegym/deployment" "See how IdeGYM is deployed on Kubernetes."
    click srv "/idegym/architecture/server" "Look inside a running environment pod."
    click tools "/idegym/architecture/rewards-tools" "See what agents can do and how runs are scored."
    click ide "/idegym/architecture/plugins" "See how IDE plugins expose inspections and MCP tools."
    click registry "/idegym/architecture/image-builder" "See where built images are stored."
    click otel "/idegym/deployment" "See metrics and tracing across the stack."

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
| **Orchestrator** | FastAPI control plane: lifecycle, state, forwarding | [Orchestrator →](/architecture/orchestrator) |
| **Image Builder / Kaniko** | Compose images from plugins, build in-cluster | [Image builder →](/architecture/image-builder) |
| **Server Pod** | Sandboxed FastAPI environment + MCP gateway | [Server →](/architecture/server) |
| **Clients** | Python client library + MCP access for agents | [Client →](/architecture/client) |
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
