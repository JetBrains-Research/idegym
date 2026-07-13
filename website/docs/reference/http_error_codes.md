# HTTP Error Status Codes Reference

This document describes the HTTP status codes used by the two main IdeGYM services:
the **Orchestrator** (Kubernetes orchestration service) and the **IdeGYM Server** (in-container tool/reward API).

---

## Async Operation Pattern (Orchestrator)

Most orchestrator endpoints that trigger long-running work are **asynchronous**: the HTTP response is returned immediately with `202 Accepted` and an `operation_id`, while the actual work runs in the background. The final outcome is retrieved by polling:

```
GET /api/operations/status/{operation_id}
```

For these endpoints, **HTTP-level error codes (4xx/5xx) only cover request validation failures** that are detected synchronously (e.g. client not found, resource limit exceeded). Any error that occurs during background execution is reported via the operation status response fields:

| Field | Type | Description |
|-------|------|-------------|
| `status` | `AsyncOperationStatus` | `SCHEDULED`, `IN_PROGRESS`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `FINISHED_BY_WATCHER` |
| `result.status_code` | `int \| null` | HTTP-equivalent error code when `status == FAILED` or `CANCELLED` |
| `result.body` | `str \| null` | Error detail message |

---

## Orchestrator Endpoints

### Diagnostics

| Method | Path | Success | Error |
|--------|------|---------|-------|
| `GET` | `/health` | `200 OK` | — |
| `GET` | `/metrics` | `200 OK` (OpenMetrics) | — |

### Clients — `POST /api/clients`

Register or re-register a client. If node provisioning is needed, an async operation is started.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Client registered, no nodes to spin up | `200 OK` | `RegisteredClientResponse`, no `operation_id` |
| Client registered, nodes being provisioned | `202 Accepted` | `RegisteredClientResponse` with `operation_id` for polling |
| Internal error | `500 Internal Server Error` | JSON `detail` with error description |

On success, `status == SUCCEEDED` and `result` contains the `RegisteredClientResponse` body.

**Async operation non-success outcomes** (polled via `GET /api/operations/status/{operation_id}`):

| Scenario | `result.status_code` | `status` |
|----------|----------------------|----------|
| Node provisioning failed | `500` | `FAILED` |
| Operation cancelled | `499` | `CANCELLED` |

### Clients — `POST /api/clients/heartbeat`

Update client availability status.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Success | `200 OK` | `RegisteredClientResponse` |
| Client not found | `404 Not Found` | `detail: "Client with ID {id} not found"` |
| Internal error | `500 Internal Server Error` | JSON `detail` |

### Clients — `DELETE /api/clients`

Stop a client and all its associated servers. Always async.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Accepted | `202 Accepted` | `StopClientResponse` with `operation_id` for polling |
| Client not found | `404 Not Found` | `detail: "Client with ID {id} not found"` |
| Internal error | `500 Internal Server Error` | JSON `detail` |

On success, `status == SUCCEEDED` and `result` contains the `RegisteredClientResponse` body.

**Async operation non-success outcomes**:

| Scenario | `result.status_code` | `status` |
|----------|----------------------|----------|
| One or more servers/nodes failed to stop | `500` | `FAILED` |
| Operation cancelled | `499` | `CANCELLED` |

### Clients — `POST /api/clients/finish`

Mark a client and its servers as finished (available for reuse by other clients).

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Success | `200 OK` | `RegisteredClientResponse` |
| Client not found | `404 Not Found` | `detail: "Client with ID {id} not found"` |
| Internal error | `500 Internal Server Error` | JSON `detail` |

### Servers — `POST /api/idegym-servers`

Start a new IdeGYM server (or reuse a finished one). Always async.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Accepted | `202 Accepted` | `StartServerResponse` with `operation_id` |
| Client not found | `404 Not Found` | `detail: "Client with ID {id} not found"` |
| Resource limit exceeded | `429 Too Many Requests` | `detail: "Resource limit exceeded..."` |
| FIFO queue blocked | `429 Too Many Requests` | `detail: "Server reuse blocked due to pending START_SERVER operations..."` |
| Internal error | `500 Internal Server Error` | JSON `detail` |

On success, `status == SUCCEEDED` and `result` contains the `StartServerResponse` body.

**Async operation non-success outcomes**:

| Scenario | `result.status_code` | `status` |
|----------|----------------------|----------|
| Server failed to start (K8s/timeout error) | `500` | `FAILED` |
| HTTP validation error during startup | varies (4xx/5xx) | `FAILED` |
| Operation cancelled | `499` | `CANCELLED` |

### Servers — `DELETE /api/idegym-servers`

Stop an IdeGYM server and clean up its Kubernetes resources. Always async.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Accepted | `202 Accepted` | `ServerActionResponse` with `operation_id` |
| Client not found | `404 Not Found` | `detail: "Client with ID {id} not found"` |
| Server not found | `404 Not Found` | `detail: "IdeGYM server with ID {id} not found"` |
| Server not active | `410 Gone` | `detail: "IdeGYM server with ID {id} is not available (status: ...)"` |
| Internal error | `500 Internal Server Error` | JSON `detail` |

On success, `status == SUCCEEDED` and `result` contains the `ServerActionResponse` body.

**Async operation non-success outcomes**:

| Scenario | `result.status_code` | `status` |
|----------|----------------------|----------|
| Kubernetes cleanup failed | `500` | `FAILED` |
| Operation cancelled | `499` | `CANCELLED` |

### Servers — `POST /api/idegym-servers/finish`

Mark a server as finished (not stopped — pod remains running, available for reuse).

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Success | `200 OK` | `ServerActionResponse` |
| Client not found | `404 Not Found` | `detail: "Client with ID {id} not found"` |
| Server not found | `404 Not Found` | `detail: "IdeGYM server with ID {id} not found"` |
| Server not active | `410 Gone` | `detail: "IdeGYM server with ID {id} is not available (status: ...)"` |
| Internal error | `500 Internal Server Error` | JSON `detail` |

### Servers — `POST /api/idegym-servers/restart`

Restart an IdeGYM server's pods. Always async.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Accepted | `202 Accepted` | `ServerActionResponse` with `operation_id` |
| Client not found | `404 Not Found` | |
| Server not found / not active | `404 Not Found` / `410 Gone` | |
| Internal error | `500 Internal Server Error` | JSON `detail` |

On success, `status == SUCCEEDED` and `result` contains the `ServerActionResponse` body.

**Async operation non-success outcomes**:

| Scenario | `result.status_code` | `status` |
|----------|----------------------|----------|
| Restart failed | `500` | `FAILED` |
| Operation cancelled | `499` | `CANCELLED` |

### Forwarding — `GET|POST|PUT|DELETE|PATCH /api/forward/{client_id}/{server_id}/{path}`

Forward an HTTP request to an IdeGYM server's tool/reward API. Always async.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Accepted | `202 Accepted` | `ForwardRequestResponse` with `async_operation_id` |
| Client not found | `404 Not Found` | |
| Server not found / not active | `404 Not Found` / `410 Gone` | |
| Internal error | `500 Internal Server Error` | JSON `detail` |

Unlike other async endpoints, `result` always carries the upstream HTTP status code regardless of outcome.

**Async operation outcomes**:

| Scenario | `result.status_code` | `status` |
|----------|----------------------|----------|
| Request forwarded successfully | upstream status (2xx/3xx) | `SUCCEEDED` |
| Upstream returned 4xx error | upstream 4xx | `FAILED` |
| Upstream returned 5xx error | upstream 5xx | `FAILED` |
| Cannot connect to server | `410` | `FAILED` |
| Client disconnected mid-stream | `499` | `CANCELLED` |
| Internal forwarding error | `500` | `FAILED` |

### Forwarding — `WS /api/ws-forward/{client_id}/{server_id}/ws`

Upgrade an HTTP connection to a WebSocket and relay it to the IdeGYM server.

| Scenario | HTTP/WS Status | Details |
|----------|----------------|---------|
| Upgrade accepted | `101 Switching Protocols` | Bidirectional relay established |
| Client not found | `404 Not Found` | HTTP response before upgrade |
| Server not found / not active | `404 Not Found` / `410 Gone` | HTTP response before upgrade |
| Internal error (post-upgrade) | WS close `1011 Internal Error` | WebSocket close frame |

### Operations — `GET /api/operations/status/{operation_id}`

Poll the status of an async operation.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Success | `200 OK` | `AsyncOperationStatusResponse` |
| Operation not found | `404 Not Found` | `detail: "Operation with ID {id} not found"` |
| Internal error | `500 Internal Server Error` | JSON `detail` |

### Build Images — `POST /api/build-push-images`

Start Kaniko image build jobs from a YAML spec.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Jobs started | `200 OK` | `BuildFromYamlResponse` with `job_names: list[str]` |
| Internal error | `500 Internal Server Error` | JSON `detail` |

> Note: This endpoint does **not** use the async operation pattern. Job status must be polled separately via `GET /api/jobs/status/{job_name}`.

### Build Images — `GET /api/jobs/status/{job_name}`

Get Kaniko job status by job name.

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Success | `200 OK` | `JobStatusResponse` |
| Job not found | `404 Not Found` | `detail: "Kaniko job with name {name} not found"` |
| Internal error | `500 Internal Server Error` | JSON `detail` |

---

## IdeGYM Server Endpoints

The IdeGYM Server runs inside each container and provides synchronous tool and reward APIs. All responses are **synchronous** — no async operation polling is required.

### Global Exception Handlers

These handlers apply to all server endpoints:

| Exception Type | HTTP Status |
|----------------|-------------|
| `FileNotFoundError` | `404 Not Found` |
| `PermissionError` | `403 Forbidden` |
| `OSError` (other) | `400 Bad Request` |
| `BashCommandExecutionTimeoutError` | `500 Internal Server Error` |
| `Exception` (catch-all) | `500 Internal Server Error` |

All error responses include a JSON body: `{"timestamp": "...", "message": "...", "traceback": "..."}`.

### Actuator

| Method | Path | Success | Error |
|--------|------|---------|-------|
| `GET` | `/api/health` | `200 OK` (empty body) | — |
| `GET` | `/api/metrics` | `200 OK` (OpenMetrics) | — |
| `GET` | `/api/log` | `200 OK` (streaming binary) | `404` if log file missing |
| `GET` | `/api` | `302 Found` → `/api/health` | — |
| `POST` | `/api/shutdown` | `202 Accepted` | — |

### Tools — Bash

| Method | Path | Success | Error |
|--------|------|---------|-------|
| `POST` | `/api/tools/bash` | `200 OK` | `500` on timeout; bash failure is in `exit_code` |

Response: `{stdout, stderr, exit_code}`. A non-zero `exit_code` does **not** produce an HTTP error — it is encoded in the response body.

### Tools — File Operations

| Method | Path | Success | Error |
|--------|------|---------|-------|
| `POST` | `/api/tools/file/create` | `200 OK` `{status: "SUCCESS"}` | `400`/`403`/`404`/`500` via global handlers |
| `POST` | `/api/tools/file/edit` | `200 OK` `{status: "SUCCESS"}` | `400`/`403`/`404`/`500` via global handlers |
| `POST` | `/api/tools/file/patch` | `200 OK` `{status: "SUCCESS"}` | `400`/`403`/`404`/`500` via global handlers |

### Filesystem API

Path validation is performed by the `valid_workspace_path` dependency for all `/api/fs/*` endpoints before the handler runs:

| Validation Failure | HTTP Status |
|--------------------|-------------|
| Invalid path (resolution error) | `400 Bad Request` |
| Path escapes workspace root | `403 Forbidden` |

#### `GET /api/fs/ls/{path}` — List directory

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Success | `200 OK` | JSON array of entry names |
| Path not found | `404 Not Found` | `detail: "Path not found: {path}"` |
| Path is not a directory | `400 Bad Request` | `detail: "Path is not a directory: {path}"` |
| Path escapes workspace | `403 Forbidden` | — |
| Path resolution error | `400 Bad Request` | error string |

#### `GET /api/fs/cat/{path}` — Read file

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Success | `200 OK` | Streaming binary content |
| Path not found | `404 Not Found` | `detail: "Path not found: {path}"` |
| Path is not a file | `400 Bad Request` | `detail: "Path is not a file: {path}"` |
| Path escapes workspace | `403 Forbidden` | — |

#### `PUT /api/fs/touch/{path}` — Create or touch file

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| File created | `201 Created` | empty body |
| File already exists | `200 OK` | empty body |
| Parent directory not found | `404 Not Found` | — |
| Path escapes workspace | `403 Forbidden` | — |
| File creation failed | `500 Internal Server Error` | — |

#### `PUT /api/fs/mkdir/{path}` — Create directory

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Directory created | `201 Created` | empty body |
| Directory already exists | `200 OK` | empty body |
| Path exists but is not a directory | `400 Bad Request` | — |
| Parent directory not found (no `parents` flag) | `404 Not Found` | — |
| Path escapes workspace | `403 Forbidden` | — |

#### `DELETE /api/fs/rm/{path}` — Delete file or directory

| Scenario | HTTP Status | Details |
|----------|-------------|---------|
| Deleted successfully | `200 OK` | empty body |
| Path not found | `404 Not Found` | — |
| Attempt to delete workspace root | `403 Forbidden` | — |
| Directory not empty and `recursive=false` | `400 Bad Request` | `detail: "Directory is not empty. Use recursive=true to delete it."` |
| Path escapes workspace | `403 Forbidden` | — |

### Rewards

Reward endpoints always return `200 OK`. Success or failure of the underlying operation is encoded in the `status` field of the response body (`SUCCESS`, `FAILURE`, `IN_PROGRESS`). HTTP errors are only raised for infrastructure failures (timeout, OS errors, exceptions) via global handlers.

| Method | Path | Success | Error |
|--------|------|---------|-------|
| `POST` | `/api/rewards/setup` | `200 OK` `{status, output}` | `500` on timeout/exception |
| `POST` | `/api/rewards/compilation` | `200 OK` `{status, output}` | `500` on timeout/exception |
| `POST` | `/api/rewards/test` | `200 OK` `{status, scores}` | `500` on timeout/exception |

### Project

| Method | Path | Success | Error |
|--------|------|---------|-------|
| `POST` | `/api/project/reset` | `200 OK` `{status, output}` | `500` on timeout/exception |

> Note: If no archive is configured for the project, the response is `200 OK` with `status: "FAILURE"` and `output: "Can not reset project without an archive!"`. This is a server misconfiguration and should ideally return a 5xx status, but the current contract preserves `200 OK` to keep the response format consistent with the reward endpoints.

---

## Status Code Reference Summary

| Code | Meaning | Where Used |
|------|---------|------------|
| `200 OK` | Success (synchronous) | All synchronous successful responses |
| `201 Created` | Resource created | `PUT /api/fs/touch`, `PUT /api/fs/mkdir` |
| `202 Accepted` | Accepted for async processing | All async orchestrator endpoints; `POST /api/shutdown` |
| `302 Found` | Redirect | `GET /api` |
| `307 Temporary Redirect` | Redirect | `/dashboard` → `/dashboard/servers` |
| `400 Bad Request` | Invalid request or bad path | Path not a directory/file, bad workspace path, non-empty dir delete |
| `403 Forbidden` | Access denied | Path escapes workspace, `PermissionError`, deleting workspace root |
| `404 Not Found` | Resource missing | Client/server/operation/job not found, path not found |
| `410 Gone` | Server no longer available | Server status is terminal (stopped/crashed/killed/etc.) |
| `429 Too Many Requests` | Resource limit exceeded | Resource quota full, FIFO queue blocked |
| `499` (non-standard) | Client closed connection | Stored in `result.status_code` of cancelled async operations |
| `500 Internal Server Error` | Unexpected server error | Unhandled exceptions, K8s errors, bash timeout |
