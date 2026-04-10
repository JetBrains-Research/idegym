# Local Deployment Guide

This guide explains how to run the full IdeGYM stack locally on macOS using Minikube.

Two approaches are covered:

1. **[Using pre-built images from GHCR](#approach-1-pre-built-ghcr-images)** — the fastest way to get started; pulls
   ready-made orchestrator and server images from GitHub Container Registry.
2. **[Building images locally](#approach-2-building-images-locally)** — builds all images from source and loads them
   directly into Minikube, with no dependency on an external registry. This mirrors what the e2e test suite does.

---

## Prerequisites

Install the required tools. This guide assumes [Homebrew](https://brew.sh) is available.

### Docker

```shell
brew install --cask docker-desktop
# or the CLI-only formula
brew install docker
```

Start Docker Desktop and wait for it to be ready.

### Kubernetes tools

```shell
brew install kubernetes-cli minikube
```

Verify the installations:

```shell
kubectl version --client
minikube version
```

### uv (Python package manager)

```shell
brew install uv
```

Or use the official installer:

```shell
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Project dependencies

From the repository root:

```shell
uv python install
uv venv --seed
uv sync --all-packages --all-extras --all-groups
```

---

## Start the Minikube Cluster

Both deployment approaches use the same cluster setup.

### Approach 1: Pre-built GHCR images (no local registry needed)

```shell
minikube start \
  --addons=gvisor,ingress \
  --container-runtime=containerd \
  --docker-opt containerd=/var/run/containerd/containerd.sock \
  --kubernetes-version=v1.35.0
```

### Approach 2: Local image builds (adds the registry addon)

```shell
minikube start \
  --addons=gvisor,ingress,registry \
  --container-runtime=containerd \
  --docker-opt containerd=/var/run/containerd/containerd.sock \
  --kubernetes-version=v1.35.0
```

The `registry` addon creates a cluster-internal Docker registry at
`registry.kube-system.svc.cluster.local`. Kaniko pods push built images here.

### Create the namespace

```shell
kubectl create namespace idegym
```

> [!NOTE]
> While not mandatory, we also recommend the
> [Kubernetes plugin for IDEA](https://plugins.jetbrains.com/plugin/10485-kubernetes)
> for browsing cluster resources from the IDE.

---

## Approach 1: Pre-built GHCR Images

This is the simplest way to run IdeGYM locally. The orchestrator and server images are pulled directly from
[GitHub Container Registry](https://github.com/orgs/JetBrains-Research/packages?ecosystem=container).

### Configure Docker registry access

Create a [GitHub PAT](https://github.com/settings/tokens) with the `read:packages` scope, then register it
as a Kubernetes secret:

```shell
kubectl create secret docker-registry regcred \
  --docker-server=ghcr.io \
  --docker-username=<your-github-username> \
  --docker-password=<ghp_your_token> \
  --namespace=idegym
```

> [!TIP]
> Verify the secret was created:
> ```shell
> kubectl get secrets -n idegym
> ```

### Deploy all resources

All Kubernetes manifests live under `orchestrator/kubernetes/`. Deploy everything at once with Kustomize:

```shell
kubectl apply -k orchestrator/kubernetes/
```

Or deploy components individually:

```shell
# Database
kubectl apply -f orchestrator/kubernetes/postgresql/ -n idegym

# Observability
kubectl apply -f orchestrator/kubernetes/tempo/ -n idegym
kubectl apply -f orchestrator/kubernetes/prometheus/ -n idegym
kubectl apply -f orchestrator/kubernetes/grafana/ -n idegym

# Orchestrator
kubectl apply -f orchestrator/kubernetes/orchestrator/ -n idegym
```

### Expose the services

Add the orchestrator hostname to your `/etc/hosts`:

```shell
echo "127.0.0.1 idegym.test" | sudo tee -a /etc/hosts
```

> [!NOTE]
> This only needs to be done once — the entry persists across cluster restarts.

In a **separate terminal window**, start the Minikube tunnel:

```shell
sudo minikube tunnel
```

> [!WARNING]
> Keep this terminal open. The tunnel must stay active for services to be reachable.

Verify the orchestrator is up:

```shell
curl idegym.test/health
# → {"status":"healthy"}
```

---

## Approach 2: Building Images Locally

This approach builds all images from source and loads them into Minikube — no external registry or credentials
required. The e2e test suite uses this exact flow.

The key differences from Approach 1:
- The `registry` Minikube addon is required (for Kaniko builds inside the cluster)
- A separate hostname (`idegym-local.test`) is used to avoid conflicts with Approach 1
- Images are built locally and loaded into Minikube with `minikube image load`

### Build and load images

**Base server image** (Debian bookworm, used as the base for environment containers):

```shell
uv run python scripts/build_server_images.py
```

This builds the image from `Dockerfile.jinja` and tags it as
`ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest`.
Then load it into Minikube:

```shell
minikube image load ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest
```

**Orchestrator image**:

```shell
uv run python scripts/build_orchestrator_image.py
```

This builds and automatically loads the orchestrator image into Minikube.

Verify images are available:

```shell
minikube image ls | grep idegym
```

### Push the base server image to the cluster registry

Kaniko jobs inside the cluster need to pull the base server image from the cluster-internal registry.
Use a privileged job to push it from Minikube's containerd to the registry:

```shell
# This step is handled automatically by the e2e test suite (utils/build_images.py).
# For manual use, see the registry-push-job pattern in e2e-tests/utils/build_images.py.
```

> [!NOTE]
> The e2e tests handle all of this automatically. If you just want to run the test suite,
> follow [E2E Tests](../e2e-tests/README.md) instead.

### Deploy the local kustomize overlay

The e2e tests provide a kustomize overlay that configures the cluster for local builds:

```shell
kubectl apply -k e2e-tests/config/
```

This overlay:
- Sets the namespace to `idegym-local`
- Configures `ImagePullPolicy: IfNotPresent` (uses locally loaded images)
- Sets the ingress host to `idegym-local.test`
- Configures Kaniko to use the cluster-internal registry

### Configure host access

```shell
echo "127.0.0.1 idegym-local.test" | sudo tee -a /etc/hosts
```

In a **separate terminal**, start the tunnel:

```shell
sudo minikube tunnel
```

Verify:

```shell
curl http://idegym-local.test/health
# → {"status":"healthy"}
```

---

## Deploying Changes to the Orchestrator

When you modify orchestrator code and want to test it in the cluster:

```shell
# Delete the existing deployment
kubectl delete -f orchestrator/kubernetes/orchestrator/deployment.yaml -n idegym

# Rebuild and load the new image
uv run python scripts/build_orchestrator_image.py

# Re-deploy
kubectl apply -f orchestrator/kubernetes/orchestrator/deployment.yaml -n idegym
```

The build script also accepts flags:
- `--push` — push the built image to the remote registry
- `--no-cache` — disable Docker layer cache
- `--multiplatform` — build for `linux/amd64` and `linux/arm64`

> [!NOTE]
> If your machine architecture differs from the cluster (e.g., Apple Silicon building for amd64),
> use `--multiplatform`. See [Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/).

---

## Accessing Metrics and Traces

Port-forward the Grafana service:

```shell
kubectl port-forward svc/grafana 3000:3000 -n idegym
```

Then open [http://localhost:3000](http://localhost:3000) (default credentials: `admin` / `changeme`).

Grafana is pre-configured with:
- **Prometheus** datasource — application and infrastructure metrics
- **Tempo** datasource — distributed traces

---

## Cluster Cleanup

```shell
# Remove all resources in the namespace
kubectl delete namespace idegym

# Stop Minikube
minikube stop

# Delete the cluster entirely (optional)
minikube delete
```

---

## Troubleshooting

### Pods in `ImagePullBackOff`

The image is not available in Minikube. Check what's loaded:

```shell
minikube image ls | grep idegym
```

Then rebuild and reload as described in [Build and load images](#build-and-load-images).

### `curl idegym.test/health` times out

1. Confirm the tunnel is running:
   ```shell
   ps aux | grep "minikube tunnel"
   ```
2. Confirm the ingress controller has an external IP:
   ```shell
   kubectl get svc -n ingress-nginx ingress-nginx-controller
   # EXTERNAL-IP should show 127.0.0.1
   ```
3. Confirm the orchestrator pod is running:
   ```shell
   kubectl get pods -n idegym
   ```

### Authentication errors when pulling from GHCR

Verify the `regcred` secret exists and is referenced by the orchestrator service account:

```shell
kubectl get secret regcred -n idegym
kubectl get serviceaccount -n idegym -o yaml | grep imagePullSecrets
```
