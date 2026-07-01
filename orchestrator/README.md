# IdeGYM Orchestrator

FastAPI service that manages IdeGYM server pods in Kubernetes: registers clients, starts and stops server deployments, forwards HTTP and WebSocket requests, and tracks Kaniko image builds. State is stored in PostgreSQL.

## Features

- Register clients with optional node pre-provisioning
- Start, stop, finish, and restart IdeGYM server pods
- Server reuse strategies to avoid cold starts between RL episodes
- Async operation tracking — long-running actions return immediately with an `operation_id`
- HTTP and WebSocket request forwarding to running servers
- Kaniko image build orchestration (triggered from YAML)
- MCP server for tool-based access to orchestrator operations
- Prometheus metrics endpoint
- Web dashboard for live monitoring of servers, clients, pods, and resource rules

## Authentication

The orchestrator uses HTTP Basic Authentication. Set `IDEGYM_AUTH_USERNAME` and `IDEGYM_AUTH_PASSWORD` as environment variables (or Kubernetes secret refs) on the orchestrator pod. All API requests must include a matching `Authorization: Basic ...` header.

The client library (`IdeGYMClient`) handles this automatically when initialized with `username` and `password`.

---

## MCP Server

The orchestrator exposes an MCP server at `/mcp`. It uses the same HTTP Basic Authentication as the REST API and
provides tool-based access to client, server, forwarding, async operation, and Kaniko build operations.

See [MCP Server](../website/docs/reference/mcp.md) for connection examples, available tools, and lifecycle examples.

---

## Async Operation Pattern

Most mutating endpoints execute their work in a background task and return immediately with an `operation_id`:

```json
{ "operation_id": 42, ... }
```

Poll `GET /api/operations/status/{operation_id}` to track progress. The `status` field progresses through:

`SCHEDULED` → `IN_PROGRESS` → `SUCCEEDED` (or `FAILED` / `CANCELLED`)

When `SUCCEEDED`, the `result` field contains the JSON-serialized final response (e.g. `StartServerResponse`).

---

## API Reference

### Health & Diagnostics

```
GET /health
```

Returns `{"status": "healthy"}`. Use for readiness/liveness probes.

```
GET /metrics
```

Returns Prometheus metrics in OpenMetrics format (multiprocess-safe).

---

### Client Management

#### Register client

```
POST /api/clients
```

Creates a new client record. Optionally pre-provisions Kubernetes nodes.

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | required | Human-readable client name |
| `nodes_count` | int | `0` | Number of nodes to spin up immediately |
| `namespace` | string | `"idegym"` | Kubernetes namespace |

**Response:** `RegisteredClientResponse` (see below). If `nodes_count > 0`, also includes `operation_id` for tracking the node spin-up.

#### Send heartbeat

```
POST /api/clients/heartbeat
```

Updates the client's last-seen timestamp and availability status.

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `client_id` | UUID | required | Client ID |
| `availability` | string | `"ALIVE"` | New availability status |

#### Finish client (soft shutdown)

```
POST /api/clients/finish
```

Marks the client and all its ALIVE servers as `FINISHED` — no Kubernetes resources are deleted. Finished servers become eligible for reuse by future clients (see [Server Reuse](#server-reuse)).

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `client_id` | UUID | required | Client ID |
| `namespace` | string | `"idegym"` | Kubernetes namespace |

**Response:** `RegisteredClientResponse` with `availability: "FINISHED"`.

#### Stop client (full teardown)

```
DELETE /api/clients
```

Stops all alive servers (deletes Kubernetes deployments and services), then marks the client as `STOPPED`. Returns immediately with an `operation_id`; poll to confirm completion.

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `client_id` | UUID | required | Client ID |
| `namespace` | string | `"idegym"` | Kubernetes namespace |

**Response:**
```json
{ "operation_id": 42 }
```

---

#### `RegisteredClientResponse` schema

```json
{
  "id": "uuid",
  "name": "my-client",
  "namespace": "idegym",
  "nodes_count": 2,
  "last_heartbeat_time": 1234567890123,
  "availability": "ALIVE",
  "created_at": 1234567890000,
  "operation_id": null
}
```

---

### Server Management

#### Start server

```
POST /api/idegym-servers
```

Creates a Kubernetes Deployment + Service for an IdeGYM server and waits for pods to become ready. Returns immediately with an `operation_id`; the final `StartServerResponse` (with `server_id` etc.) is available once the operation `SUCCEEDED`.

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `client_id` | UUID | required | Owning client |
| `namespace` | string | `"idegym"` | Kubernetes namespace |
| `image_tag` | string | required | OCI image to deploy |
| `server_name` | string | `"default-idegym-server"` | Logical name (used as K8s name prefix) |
| `runtime_class_name` | string\|null | `null` | Kubernetes RuntimeClass (e.g. `"gvisor"`) |
| `run_as_root` | bool | `false` | Run container as UID 0 |
| `service_port` | int | `80` | Port exposed by the Kubernetes Service |
| `container_port` | int | `8000` | Port the container listens on |
| `resources` | object\|null | `null` | K8s resource requirements (`requests`/`limits` dict) |
| `node_selector` | object\|null | `null` | Kubernetes node selector labels |
| `server_start_wait_timeout_in_seconds` | int | `60` | How long to wait for pods to be ready |
| `reuse_strategy` | string | `"RESET"` | Server reuse strategy — see [Server Reuse](#server-reuse) |
| `server_kind` | string | `"idegym"` | Server type: `"idegym"` or `"openenv"` |

**Immediate response:**
```json
{
  "namespace": "idegym",
  "client_id": "uuid",
  "operation_id": 42
}
```

**Final result** (available via `GET /api/operations/status/42` once SUCCEEDED):
```json
{
  "namespace": "idegym",
  "client_id": "uuid",
  "server_id": 7,
  "server_name": "my-server",
  "generated_name": "my-client-my-server-7",
  "image_tag": "registry.example.com/my-image:abc123",
  "need_to_reset": false,
  "operation_id": 42
}
```

`need_to_reset: true` means the server was reused via `RESET` strategy and the caller is responsible for resetting project state.

#### Finish server (soft shutdown)

```
POST /api/idegym-servers/finish
```

Marks the server as `FINISHED` without touching Kubernetes resources. The pod keeps running and the server becomes eligible for reuse.

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `client_id` | UUID | required | Owning client |
| `namespace` | string | `"idegym"` | Kubernetes namespace |
| `server_id` | int | required | Server ID |

**Response:**
```json
{
  "server_name": "my-client-my-server-7",
  "message": "Finished IdeGYM server my-client-my-server-7 (available for reuse)"
}
```

#### Stop server (full teardown)

```
DELETE /api/idegym-servers
```

Deletes the Kubernetes Deployment and Service and marks the server `STOPPED`. Returns immediately with an `operation_id`.

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `client_id` | UUID | required | Owning client |
| `namespace` | string | `"idegym"` | Kubernetes namespace |
| `server_id` | int | required | Server ID |

**Response:**
```json
{
  "server_name": "my-client-my-server-7",
  "message": "Stop initiated for my-client-my-server-7",
  "operation_id": 43
}
```

#### Restart server

```
POST /api/idegym-servers/restart
```

Deletes pods (keeps Deployment and Service) and waits for them to come back. Useful for resetting process state without changing the image. Returns immediately with an `operation_id`.

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `client_id` | UUID | required | Owning client |
| `namespace` | string | `"idegym"` | Kubernetes namespace |
| `server_id` | int | required | Server ID |
| `server_start_wait_timeout_in_seconds` | int | `60` | How long to wait for pods to be ready again |

---

### Server Reuse

The `reuse_strategy` field on `StartServerRequest` controls whether the orchestrator tries to reuse an existing `FINISHED` server instead of deploying a new one:

| Strategy | Behaviour |
|----------|-----------|
| `NONE` | Always create a new server |
| `RESET` | Claim the first matching `FINISHED` server; pods are left running, caller resets project state. Response has `need_to_reset: true` |
| `RESTART` | Claim a `FINISHED` server and restart its pods (fresh process state) |


Servers are matched by `server_name` and `image_tag`. Use `POST /api/idegym-servers/finish` (not `DELETE`) to return a server to the reuse pool after an RL episode.

---

### Pod Snapshots (Checkpoint/Restore)



Pod snapshotting captures the full memory and filesystem state of a running server pod and persists it to object
storage. A new pod started with the same snapshot identifier resumes from that checkpoint instead of doing a cold
start.

The feature is implemented on top of [Google Kubernetes Engine pod
snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots) and only works on a GKE
cluster where the feature is enabled. See [Remote Deployment → Pod Snapshots](../website/docs/reference/remote_deployment.md)
for how to turn the feature on.

#### Constraints

- **GKE only.** The chart provisions GKE-specific CRDs and a GCS bucket holds the checkpoints.
- **gVisor runtime required.** Snapshots only work for pods running with `runtimeClassName: gvisor`.
- **Not eligible on E2 instance types.** GCP E2 nodes do not support pod snapshots.

#### How it works

Every server pod carries a `snapshot_id` that identifies its snapshot group in GCS — by default equal to the pod's
own generated name, so any future snapshot taken of that pod is associated with this label. Snapshot endpoints ask
GKE to checkpoint the running pod under that identifier; `start_server(snapshot_id=...)` deploys a new pod with the
same identifier, and GKE restores it from the checkpoint instead of doing a cold start.

For a restore to succeed, the new server's configuration must be identical to the snapshotted one — same image,
runtime class, resources, and so on. Snapshotting a server that was itself started from a snapshot overwrites the existing snapshot under the same
`snapshot_id` rather than creating a new one.

#### Snapshot an existing server

```
POST /api/idegym-servers/snapshot
```

Snapshots a server that is already running. Returns immediately with an `operation_id`; poll
`GET /api/operations/status/{operation_id}` for the final `CreateSnapshotResponse`.

**Request body:**

| Field        | Type   | Default    | Description                                                       |
|--------------|--------|------------|-------------------------------------------------------------------|
| `client_id`  | UUID   | required   | Owning client                                                     |
| `server_id`  | int    | required   | Server to snapshot — must be `ALIVE` and use `gvisor` runtime     |
| `namespace`  | string | `"idegym"` | Kubernetes namespace the server runs in                           |

**Final result** (in `result` of the async operation, once `SUCCEEDED`):

```json
{
  "server_id": 7,
  "server_name": "my-client-my-server-7",
  "snapshot_id": "my-client-my-server-7",
  "operation_id": 42
}
```

`snapshot_id` is the value to pass back as `StartServerRequest.snapshot_id` to restore from this snapshot.

#### Prepare a batch of snapshots from scratch

```
POST /api/snapshots/prepare
```

Accepts a list of `StartServerRequest`s, deploys each server, snapshots it, then tears it down — designed for
warming up a pool of snapshots ahead of an RL training run without leaving the servers running.

**Request body:**

| Field      | Type                    | Description                                                       |
|------------|-------------------------|-------------------------------------------------------------------|
| `requests` | list of `StartServerRequest` | One entry per snapshot to produce. Each must use `runtime_class_name: "gvisor"` |

**Immediate response:**

```json
{ "request_id": "uuid" }
```

Each request is processed by an independent pipeline job that updates a `snapshot_jobs` row throughout
(`IN_PROGRESS` → `SUCCESS` / `FAILURE`). On success, a row is also written to the `snapshots` table keyed by a
stable hash of `(namespace, image_tag, server_name, runtime_class_name, run_as_root, server_kind)`, so subsequent
`/api/snapshots/exists` lookups can deduplicate.

#### Poll a prepare batch

```
GET /api/snapshots/prepare/{request_id}
```

**Response:**

```json
{
  "request_id": "uuid",
  "status": "READY",
  "total_requested": 4,
  "succeeded": 3,
  "failed": 1,
  "results": [
    {
      "request_hash": "sha256...",
      "status": "success",
      "snapshot_name": "my-client-my-server-7",
      "details": null
    }
  ]
}
```

`status` is `IN_PROGRESS` until every job finishes. `results` is `null` until then; once `READY`, each entry maps
back to the original request by `request_hash` (same hash function used by `/api/snapshots/exists`).
`snapshot_name` on a successful job is the value to pass as `snapshot_id` when starting a server from it.

#### Poll a single snapshot job

```
GET /api/snapshots/jobs/{job_id}
```

Returns `{job_id, status, snapshot_name, details}` for one pipeline job. Useful when you want per-job progress
without re-fetching the whole batch.

#### Check whether a snapshot exists

```
GET /api/snapshots/exists?image_tag=...&server_name=...&runtime_class_name=gvisor&...
```

Looks up the `snapshots` table by the same configuration hash used by `/api/snapshots/prepare`. Use this before
calling `prepare` to skip work that has already been done.

**Response:**

```json
{ "exists": true, "snapshot_name": "my-client-my-server-7" }
```

#### Restoring from a snapshot

Pass `snapshot_id` on `POST /api/idegym-servers` (or the `snapshot_id` argument on `IdeGYMClient.start_server`).
The value is whatever a previous snapshot call returned as `snapshot_name` / `snapshot_id`. The new pod is created
with `idegym.jetbrains.com/snapshot-id=<that value>`, GKE matches it against its stored snapshots, and the pod
boots from the checkpoint.

---

### Request Forwarding

#### HTTP forwarding

```
ANY /api/forward/{client_id}/{server_id}/{path}
```

Supported methods: `GET`, `POST`, `PUT`, `DELETE`, `OPTIONS`, `HEAD`, `PATCH`.

Forwards the request to `http://{generated_name}.{namespace}.svc:{service_port}/{path}`, strips the `Host` and `Authorization` headers, and preserves all others. Returns immediately with an `operation_id`; the response body and headers are available via `GET /api/operations/status/{operation_id}` once `SUCCEEDED`.

Calls to paths starting with `api/tools` or `api/rewards` also update the server heartbeat.

#### WebSocket forwarding

```
WS /api/ws-forward/{client_id}/{server_id}/ws
```

Bidirectionally proxies a WebSocket connection to `ws://{generated_name}.{namespace}.svc:{service_port}/ws`. Each message received from the upstream server updates the server heartbeat.

---

### Image Building

Image builds are executed as Kaniko jobs inside the cluster. See [Image Builder](../website/docs/reference/image_builder.md) for how to define images.

#### Start build jobs

```
POST /api/build-push-images
```

Parses the provided YAML, launches one Kaniko Kubernetes Job per image, and returns job names immediately (fire-and-forget — poll individual job status separately).

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `namespace` | string | `"idegym"` | Kubernetes namespace for the Kaniko jobs |
| `yaml_content` | string | required | YAML string with `images:` list |

**Response:**
```json
{
  "job_names": ["kaniko-job-abc123", "kaniko-job-def456"]
}
```

#### Get job status

```
GET /api/jobs/status/{job_name}
```

Returns the status and pushed image tag for a single Kaniko job.

**Response:**

```json
{
  "id": 1,
  "job_name": "kaniko-job-abc123",
  "status": "SUCCESS",
  "tag": "registry.example.com/my-image:abc123",
  "details": null,
  "created_at": 1234567890000,
  "updated_at": 1234567890123
}
```

`status` values: `IN_PROGRESS`, `SUCCESS`, `FAILED`.

---

### Async Operations

```
GET /api/operations/status/{operation_id}
```

Returns the current state of an async operation.

**Response:**

```json
{
  "id": 42,
  "request_type": "START_SERVER",
  "status": "SUCCEEDED",
  "request": "{...}",
  "result": "{\"server_id\": 7, ...}",
  "client_id": "uuid",
  "server_id": 7,
  "orchestrator_pod": "orchestrator-abc-123",
  "scheduled_at": 1234567890000,
  "started_at": 1234567890010,
  "finished_at": 1234567890800
}
```

Operation types: `START_SERVER`, `STOP_SERVER`, `RESTART_SERVER`, `STOP_CLIENT`, `FORWARD_REQUEST`, `REGISTER_CLIENT_WITH_NODES`.

Status values: `SCHEDULED`, `IN_PROGRESS`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `FINISHED_BY_WATCHER`.

---

### Dashboard

The orchestrator ships a lightweight HTML dashboard for monitoring:

| Path | Description |
|------|-------------|
| `GET /` | Dashboard home |
| `GET /dashboard/servers` | Running servers with status and image |
| `GET /dashboard/clients` | Alive clients with heartbeat times |
| `GET /dashboard/pods` | Live Kubernetes pods (paginated, filterable by label selector) |
| `GET /dashboard/rules` | Resource limit rules |

---

## Database Models

### Client

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID (PK) | Client identifier |
| `name` | string | Human-readable name |
| `namespace` | string | Kubernetes namespace |
| `nodes_count` | bigint | Pre-provisioned node count |
| `availability` | string | `ALIVE`, `FINISHED`, `STOPPED`, `DELETION_FAILED` |
| `last_heartbeat_time` | bigint | Milliseconds since epoch |
| `created_at` | bigint | Milliseconds since epoch |

### IdeGYMServer

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint (PK, auto) | Server identifier |
| `client_id` | UUID (FK) | Owning client |
| `client_name` | string | Denormalized client name |
| `server_name` | string | Logical name from request |
| `generated_name` | string (unique) | Actual Kubernetes resource name |
| `namespace` | string | Kubernetes namespace |
| `image_tag` | string | Deployed OCI image |
| `container_runtime` | string | RuntimeClass |
| `cpu` / `ram` | float | Requested CPU cores / RAM in GB |
| `run_as_root` | bool | Whether container runs as root |
| `server_kind` | string | `idegym` or `openenv` |
| `service_port` | int | Kubernetes Service port |
| `availability` | string | `ALIVE`, `FINISHED`, `STOPPED`, `FAILED_TO_START`, `CRASHED`, `KILLED`, `DELETION_FAILED`, `RESTART_FAILED` |
| `last_heartbeat_time` | bigint | Milliseconds since epoch |
| `created_at` | bigint | Milliseconds since epoch |

### AsyncOperation

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint (PK, auto) | Operation identifier |
| `request_type` | string | `START_SERVER`, `STOP_SERVER`, etc. |
| `status` | string | `SCHEDULED` → `IN_PROGRESS` → `SUCCEEDED`/`FAILED`/`CANCELLED` |
| `request` | text | Original request JSON |
| `result` | text | Final result JSON (on success) |
| `client_id` | UUID (FK) | Related client |
| `server_id` | bigint (FK) | Related server (if applicable) |
| `orchestrator_pod` | string | Pod name processing the operation |
| `scheduled_at` | bigint | Milliseconds since epoch |
| `started_at` | bigint | Milliseconds since epoch |
| `finished_at` | bigint | Milliseconds since epoch |

### JobStatusRecord

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint (PK, auto) | Record identifier |
| `job_name` | string (unique) | Kubernetes Job name |
| `status` | string | `IN_PROGRESS`, `SUCCESS`, `FAILED` |
| `tag` | string | Pushed image tag |
| `details` | text | Error details if failed |
| `request_id` | string | Optional correlation ID |
| `created_at` / `updated_at` | bigint | Milliseconds since epoch |

### ResourceLimitRule

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigint (PK, auto) | Rule identifier |
| `client_name_regex` | string (unique) | Regex matched against `client_name` |
| `pods_limit` | int | Max simultaneous pods |
| `cpu_limit` / `ram_limit` | float | Total CPU cores / RAM in GB allowed |
| `used_cpu` / `used_ram` | float | Currently tracked usage |
| `current_pods` | int | Current live pod count |
| `priority` | int | Higher priority rules are evaluated first |

---

## Environment Variables

### PostgreSQL connection

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_HOST` | `localhost` | Database host |
| `POSTGRES_PORT` | `5432` | Database port |
| `POSTGRES_USER` | `postgres` | Database user |
| `POSTGRES_PASSWORD` | `postgres` | Database password |
| `POSTGRES_DB` | `idegym` | Database name |

### Authentication

| Variable | Description |
|----------|-------------|
| `IDEGYM_AUTH_USERNAME` | Basic auth username |
| `IDEGYM_AUTH_PASSWORD` | Basic auth password |

### Image building

| Variable | Default | Description |
|----------|---------|-------------|
| `KANIKO_INSECURE_REGISTRY` | `false` | Set to `"true"` to push to an HTTP (non-TLS) registry (e.g. local Minikube) |
| `DOCKER_REGISTRY` | — | Registry prefix for pushed images |

---

## Deployment

### Building the image

From the repository root:

```bash
uv run python scripts/build_server_images.py --push
```

Or build just the orchestrator image:

```bash
docker build -f orchestrator/Dockerfile -t ghcr.io/jetbrains-research/idegym/orchestrator:latest .
```

### Deploying to Kubernetes

The orchestrator and its supporting services (PostgreSQL, Prometheus, Grafana, Tempo) are
packaged as a Helm chart at [`charts/idegym`](/charts/idegym):

```bash
helm dependency update charts/idegym
helm install idegym charts/idegym -n idegym
```

See [Local Deployment](/website/docs/reference/local_deployment.md) for the full Minikube setup,
and [Remote Deployment](/website/docs/reference/remote_deployment.md) for production cluster configuration.

---

## Development

### Running locally

Install dependencies and start the orchestrator:

```bash
uv sync --all-packages --all-extras --all-groups
uv run python -m idegym.orchestrator.main
```

A PostgreSQL instance must be reachable at `POSTGRES_HOST:POSTGRES_PORT`. The Kubernetes client falls back to your local `~/.kube/config` when not running inside a cluster.

### Testing

```bash
uv run pytest -m unit
uv run pytest -m integration   # requires Docker + registry on localhost:5000
uv run pytest -m e2e            # requires a running Minikube cluster
```

See [Getting Started](../website/docs/reference/getting_started.md) for test prerequisites.
