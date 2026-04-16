# OpenEnv Integration

IdeGYM can run any [OpenEnv](https://github.com/facebookresearch/openenv)-compatible environment
as a Kubernetes pod and proxy WebSocket connections to it. This directory contains two working
examples.

| Example | HuggingFace Space | Description |
|---------|-------------------|-------------|
| `echo_env_example.py` | [openenv/echo_env](https://huggingface.co/spaces/openenv/echo_env) | MCP-based environment that echoes back messages. Good smoke-test for the full integration. |
| `tbench2_example.py` | [openenv/tbench2](https://huggingface.co/spaces/openenv/tbench2) | Terminal Bench 2 — evaluates agent solutions to terminal-based coding tasks. |

---

## How it works

```
IdeGYMClient (Python)
      │
      │  REST: register + start server
      │  ─────────────────────────────►  IdeGYM Orchestrator (Kubernetes)
      │                                         │
      │                                         │  starts Pod with OpenEnv image
      │                                         ▼
      │                                  OpenEnv Server Pod
      │
      │  WebSocket: /api/ws-forward/...
      │  ──────────────────────────────►  Orchestrator  ──► Pod
      │  (client talks to orchestrator;
      │   orchestrator forwards to pod)
```

1. `IdeGYMClient` registers with the orchestrator via REST and requests a server pod.
2. The orchestrator starts a Kubernetes pod running the OpenEnv image and returns `server.openenv_url`.
3. The OpenEnv client (`EchoEnv`, `Tbench2Env`) opens a WebSocket to the orchestrator's
   `/api/ws-forward/<client_id>/<server_id>/ws` endpoint.
4. The orchestrator proxies the WebSocket traffic to the pod — there is no direct connection
   between the client and the pod.
5. On context-manager exit the pod is stopped and all resources are released automatically.

---

## Prerequisites

- A running IdeGYM orchestrator — see [Local cluster setup](../README.md#local-cluster-setup)
  in the examples root README.
- Docker (to build the OpenEnv images below).
- Dependencies synced. From the `examples/` directory:

```bash
# examples/
uv sync
```

If the HuggingFace Spaces require authentication, log in first:

```bash
uv tool install huggingface-hub
hf auth login
# examples/
uv sync
```

---

## Step 1: Build the OpenEnv images

Run these commands from any directory. The resulting images are loaded directly into Minikube —
no external registry is needed.

### echo_env

```bash
git clone https://huggingface.co/spaces/openenv/echo_env
docker build -t echo-env:local echo_env/
minikube image load echo-env:local
```

### tbench2

```bash
git clone https://huggingface.co/spaces/openenv/tbench2
docker build -t tbench2-env:local tbench2/
minikube image load tbench2-env:local
```

---

## Step 2: Configure environment variables

From the `examples/` directory, copy the example env file and set the image tags:

```bash
# examples/
cp .env.example .env
```

Edit `examples/.env`:

```ini
ECHO_ENV_IMAGE_TAG=echo-env:local
TBENCH2_IMAGE_TAG=tbench2-env:local
```

For a remote orchestrator also set:

```ini
IDEGYM_ORCHESTRATOR_URL=https://idegym.yourdomain.com
IDEGYM_AUTH_USERNAME=admin
IDEGYM_AUTH_PASSWORD=your-password
```

---

## Step 3: Run

From the `examples/` directory:

```bash
# examples/
uv run python openenv/echo_env_example.py
uv run python openenv/tbench2_example.py
```

---

For troubleshooting see [Troubleshooting](../README.md#troubleshooting) in the examples root README.
