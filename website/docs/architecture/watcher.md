---
title: Watcher
description: The background cleanup / reconcile loop that keeps the database and the cluster in sync.
---

# Watcher

Disposable environments only stay cheap if dead ones actually go away. The **watcher** is
the background loop that guarantees this: it periodically reconciles the database against
the live Kubernetes cluster, evicts stale or crashed servers, and reclaims quota — with
no manual teardown.

## The reconcile loop (click a node for source)

```mermaid
flowchart TB
    timer["<b>⏱️ Tick · ~60s</b>"]:::infra
    lock["<b>🔒 Advisory lock</b>"]:::infra
    crash("<b>💥 Crash detection</b>"):::tool
    cleanup("<b>🧹 Cleanup / reconcile</b>"):::tool
    db[("<b>🗄️ PostgreSQL</b>")]:::store
    kapi["<b>☸️ Kubernetes</b>"]:::infra

    timer --> lock --> crash --> cleanup
    crash --> db
    crash --> kapi
    cleanup --> db
    cleanup --> kapi

    classDef tool fill:#e23b3b,stroke:#c02626,color:#fff;
    classDef store fill:#0891b2,stroke:#0e7490,color:#fff;
    classDef infra fill:#475569,stroke:#334155,color:#fff;

    click timer "https://github.com/JetBrains-Research/idegym/blob/main/watcher/src/idegym/watcher/main.py" "View the watcher loop source on GitHub."
    click crash "https://github.com/JetBrains-Research/idegym/blob/main/watcher/src/idegym/watcher/crash_detector.py" "View the crash-detector source on GitHub."
    click cleanup "https://github.com/JetBrains-Research/idegym/blob/main/watcher/src/idegym/watcher/cleanup.py" "View the cleanup / reconcile source on GitHub."
    click lock "https://github.com/JetBrains-Research/idegym/blob/main/watcher/src/idegym/watcher/cleanup.py" "See how the advisory lock is taken in cleanup on GitHub."
```

## What it does each tick

Under a Postgres **advisory lock** (so only one reconciler runs at a time), the watcher:

1. **Detects crashed servers.** Server pods are Deployments forced to
   `restartPolicy: Always`, so a crashing container would otherwise restart forever
   silently. `evaluate_pod_crash(pod, max_restarts)` (a pure function) flags a pod whose
   restart count exceeds its budget, or that is `Failed` / `Evicted`. `detect_crashed_servers`
   lists pods **once per namespace** (label selector `app.kubernetes.io/part-of=idegym`)
   — never per-server, no Events API.
2. **Tears down and records.** On a crash it deletes the Deployment first, then marks the
   server `CRASHED` (or `DELETION_FAILED` if teardown fails), recording the reason in the
   server's `details` column. The next client `forward` sees *why* in the 410 GONE detail.
3. **Cleans up the rest.** It reconciles the database against live cluster state — evicting
   servers whose clients are gone and reclaiming orphaned resources — and frees quota.

## Inactivity and the keepalive hold

A server is reaped once `inactive_timeout` (or `finished_timeout`, for a `FINISHED` one) has
passed since its `last_heartbeat_time` — which advances only when a request against it
completes. That timestamp answers "when did work last finish here", not "is anybody holding
this", and the two diverge whenever a sandbox is legitimately quiet: an agent thinking, a long
local build, a human reading a stack trace.

A client that knows it still holds a server says so with
`POST /api/idegym-servers/keepalive`, which writes an expiry into the server's
`keepalive_until` column. The watcher skips any server whose hold has not expired, before it
looks at the timeout at all. The hold is stored separately from `last_heartbeat_time` on
purpose: pushing the heartbeat into the future would keep the server alive but make "last
active" a lie, and would extend the hold by a further `inactive_timeout`.

## Restart budget

The crash policy is **per-server**: `StartServerRequest.max_restarts` (default `0` = fail
on first crash) is plumbed client → API → orchestrator → DB. It is **not** a global
orchestrator setting, so different workloads can tolerate different flakiness. Detection
latency is roughly one `cleanup_interval`.

Crash detection is gated on `WatcherConfig.crash_detection_enabled` (default on) and runs
**first** within `perform_cleanup_operations`, under the same advisory lock as the rest of
cleanup.

## Why it's a separate component

Keeping reconciliation out of the request path means the orchestrator stays responsive
while a steady background process owns convergence. The watcher reads the same
[PostgreSQL](/architecture/orchestrator) state the orchestrator writes, and acts on the
same cluster the orchestrator provisions into.

## View source

- Loop → [`watcher/src/idegym/watcher/main.py`](https://github.com/JetBrains-Research/idegym/blob/main/watcher/src/idegym/watcher/main.py)
- Cleanup / reconcile → [`watcher/src/idegym/watcher/cleanup.py`](https://github.com/JetBrains-Research/idegym/blob/main/watcher/src/idegym/watcher/cleanup.py)
- Crash detection → [`watcher/src/idegym/watcher/crash_detector.py`](https://github.com/JetBrains-Research/idegym/blob/main/watcher/src/idegym/watcher/crash_detector.py)
