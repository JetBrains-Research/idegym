---
title: Orchestrator
description: The FastAPI control plane — lifecycle management, async operations, PostgreSQL, request forwarding, and pluggable image-build submission.
---

# Orchestrator

The orchestrator is the **control plane** and the single entry point for everything.
It's a FastAPI service (deployed as a Kubernetes Deployment) that registers clients,
builds images, provisions and tears down environment pods, forwards traffic into them,
and persists all state in PostgreSQL.

## Inside the orchestrator (click a node for source)

```mermaid
flowchart TB
    subgraph app["FastAPI app"]
        mw["<b>Middleware</b><br/>tracing · task ctx"]:::infra
        subgraph routers["Routers"]
            direction TB
            rc{{"<b>client</b>"}}:::ctrl
            rs{{"<b>server</b>"}}:::ctrl
            rb{{"<b>build images</b>"}}:::ctrl
            rf{{"<b>forwarding</b>"}}:::ctrl
            ra{{"<b>async ops</b>"}}:::ctrl
            rsnap{{"<b>snapshot</b>"}}:::ctrl
        end
        mcp{{"<b>MCP</b><br/>/mcp"}}:::ctrl
    end
    pg[("<b>🗄️ PostgreSQL</b>")]:::store
    kapi["<b>Kubernetes</b>"]:::infra
    pods[["<b>Server pods</b>"]]:::pod

    routers --> pg
    rb -->|"build backend"| kapi
    rs -->|"Deployment"| kapi --> pods
    rf -->|"forward"| pods
    mcp -.->|"same handlers as REST"| routers

    classDef ctrl fill:#6b57ff,stroke:#5b4bd2,color:#fff;
    classDef store fill:#0891b2,stroke:#0e7490,color:#fff;
    classDef infra fill:#475569,stroke:#334155,color:#fff;
    classDef pod fill:#4f46e5,stroke:#4338ca,color:#fff;

    click rc "https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/router/client.py" "View the client router source on GitHub."
    click rs "https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/router/server.py" "View the server-lifecycle router source on GitHub."
    click rb "https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/router/build_images.py" "View the image-build router source on GitHub."
    click rf "https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/router/forwarding.py" "View the request-forwarding router source on GitHub."
    click ra "https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/router/async_operation.py" "View the async-operation router source on GitHub."
    click rsnap "https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/router/snapshot.py" "View the snapshot router source on GitHub."
    click mcp "https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/mcp.py" "View the MCP server source on GitHub."
    click pg "https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/database" "Browse the database layer source on GitHub."
```

## Responsibilities

- **Client lifecycle** — register a client (with optional node pre-provisioning), track
  heartbeats, and on stop/finish tear down or release its servers.
- **Server lifecycle** — start (or **reuse** a matching finished server), stop, finish
  (release for reuse), and restart environment pods, waiting for readiness. An opt-in
  preferred pod-affinity policy can group simultaneous cold starts that use the same image
  reference; normal Kubernetes image-locality scoring remains the default.
- **Image builds** — accept image-builder YAML, compile each `Image` to a spec, and submit
  it through a **pluggable build backend** (Kaniko by default, or GKE Cloud Build) driven by
  a shared `ImageBuildService` (see [image builder](/architecture/image-builder#build-backends)).
- **Forwarding** — proxy HTTP, MCP, and WebSocket requests from clients into the right
  server pod, and **persist** every request/response for offline reward computation.
- **Async operations** — long-running calls return an operation ID the client polls to
  completion; this keeps connections short and the API responsive.
- **Snapshots** — on GKE, prepare and restore pod snapshots to skip cold-start work.

## Key building blocks

| Concern | Where | Notes |
|---|---|---|
| App assembly | [`main.py`](https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/main.py) | `create_app()` builds the app, combines the FastAPI + MCP lifespans, mounts routers, instruments OpenTelemetry |
| MCP server | [`mcp.py`](https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/mcp.py) | A thin FastMCP layer over the **same** handlers as REST — no separate state |
| Routers | [`router/`](https://github.com/JetBrains-Research/idegym/tree/main/orchestrator/src/idegym/orchestrator/router) | One module per concern: `client`, `server`, `build_images`, `forwarding`, `async_operation`, `snapshot`, `dashboard`, `diagnostics` |
| State | [`database/`](https://github.com/JetBrains-Research/idegym/tree/main/orchestrator/src/idegym/orchestrator/database) | Async SQLAlchemy + asyncpg; Alembic migrations run on startup |
| Image build | [`image_build_service.py`](https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/image_build_service.py) | Backend-agnostic `ImageBuildService` drives a pluggable [`ImageBuilder`](https://github.com/JetBrains-Research/idegym/tree/main/backend-utils/src/idegym/backend/utils/image_builder) (Kaniko / GKE Cloud Build) |
| K8s resources | [`kubernetes_client.py`](https://github.com/JetBrains-Research/idegym/blob/main/backend-utils/src/idegym/backend/utils/kubernetes_client.py) | Creates Kaniko Jobs (Kaniko backend) and server Deployments via the K8s API |

## Persistence model

PostgreSQL holds **clients**, **servers**, **build jobs**, **async operations**, and
**snapshots**. Because the orchestrator stores every forwarded request and response, an
evaluation harness can recompute rewards offline and reproduce runs without re-executing
them. Concurrency-sensitive operations use real Postgres features — `FOR UPDATE SKIP
LOCKED` and advisory locks — to coordinate the watcher and request handlers safely.

## Configuration

Configuration is nested Pydantic models in `api/src/idegym/api/config.py`; `load_config()`
builds `Config` from the environment, and a variable that is unset leaves the field's default.
Environment variables are the only override mechanism — no config files, no CLI arguments.
Notable knobs include database connection, connection limits, asyncio debug/dump, the MCP
`stateless_http` mode, and node-pool scheduling. See [deployment](/deployment) for the
environment variables and Helm values that drive these.

## View source

- App & router wiring → [`orchestrator/src/idegym/orchestrator/main.py`](https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/src/idegym/orchestrator/main.py)
- Orchestrator package → [`orchestrator/`](https://github.com/JetBrains-Research/idegym/tree/main/orchestrator)
- REST reference → [`orchestrator/README.md`](https://github.com/JetBrains-Research/idegym/blob/main/orchestrator/README.md) · interactive → [API](/api)
