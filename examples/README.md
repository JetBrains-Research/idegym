# IdeGYM Examples

Runnable examples showing how to integrate external environments with IdeGYM.

`examples` is intentionally **not** part of the root uv workspace. The OpenEnv environment
packages bring in `camel-ai` → `openenv-core[core]` → `openai>=2.7.2`, which conflicts with
transitive dependencies of `idegym-backend-utils`. Keeping `examples` as a standalone project
with its own lock file lets uv resolve these dependencies independently.

## Available integrations

| Integration | Directory | Description |
|-------------|-----------|-------------|
| OpenEnv | [`openenv/`](openenv/README.md) | Run any [OpenEnv](https://github.com/meta-pytorch/OpenEnv)-compatible environment as a Kubernetes pod with WebSocket forwarding. Includes echo_env (smoke-test) and TBench2 (terminal benchmark). |

---

## Setup

From the `examples/` directory:

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

## Local cluster setup

All examples require a running IdeGYM orchestrator. The fastest way to get one locally is with
Minikube and pre-built GHCR images. See the full
[Local Deployment Guide](../documentation/local_deployment.md) for details and a locally-built
alternative.

From the **repository root**:

```bash
# repo root — start Minikube with gvisor sandbox and ingress
minikube start \
  --addons=gvisor,ingress \
  --container-runtime=containerd \
  --docker-opt containerd=/var/run/containerd/containerd.sock \
  --kubernetes-version=v1.35.0

# repo root — create namespace
kubectl create namespace idegym

# Create a GHCR pull secret (GitHub PAT with read:packages scope).
# Skip this if you build and load all images locally with
# `uv run python scripts/build_orchestrator_image.py` — see the full
# Local Deployment Guide for the locally-built approach.
kubectl create secret docker-registry regcred \
  --docker-server=ghcr.io \
  --docker-username=<your-github-username> \
  --docker-password=<ghp_your_token> \
  --namespace=idegym

# repo root — deploy orchestrator and supporting services
kubectl apply -k orchestrator/kubernetes/

# repo root — map hostname (run once, persists across restarts)
echo "127.0.0.1 idegym.test" | sudo tee -a /etc/hosts

# Start tunnel — keep this terminal open
sudo minikube tunnel
```

Verify the orchestrator is up:

```bash
curl http://idegym.test/health
# {"status":"healthy"}
```

> [!NOTE]
> The local orchestrator at `http://idegym.test` requires no authentication.

---

## Troubleshooting

### `KeyError: 'ECHO_ENV_IMAGE_TAG'` (or `TBENCH2_IMAGE_TAG`)

The `examples/.env` file is missing or doesn't contain the variable. Copy it from the template
and fill in the values, or export the variable directly:

```bash
# examples/
cp .env.example .env
# then edit .env
```

```bash
export ECHO_ENV_IMAGE_TAG=echo-env:local
```

### Pod stuck in `ImagePullBackOff`

The image tag isn't available inside the cluster. Reload it:

```bash
minikube image load echo-env:local
minikube image ls | grep echo-env   # verify
```

### `gvisor` runtime class not found

The examples set `runtime_class_name="gvisor"`. Either start Minikube with `--addons=gvisor`
(included in the command above) or remove the `runtime_class_name` argument from the example to
run without sandboxing.

### Orchestrator unreachable

Make sure `sudo minikube tunnel` is running in a separate terminal:

```bash
curl http://idegym.test/health
```

See [Local Deployment Guide — Troubleshooting](../documentation/local_deployment.md#troubleshooting)
for more.
