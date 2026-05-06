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
brew install helm kubernetes-cli minikube
```

Verify the installations:

```shell
kubectl version --client
minikube version
helm version
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

This is the simplest way to run IdeGYM locally.
The orchestrator image is pulled directly from
[GitHub Container Registry](https://github.com/orgs/JetBrains-Research/packages?ecosystem=container)
and installed with the bundled Helm chart.

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

### Create the application secrets

The chart deliberately does not manage credentials, expecting a set of pre-existing secrets in the release namespace.
Only `postgres` is required for a minimal installation, while others are needed when you opt into observability services.

#### `postgres` (required)

The orchestrator and the bundled Bitnami PostgreSQL chart both read from a single `postgres` secret.
You can create it like so:

```shell
kubectl create secret generic postgres -n idegym \
  --from-literal=host=postgres \
  --from-literal=port=5432 \
  --from-literal=database=idegym \
  --from-literal=username=idegym \
  --from-literal=password='<strong-password>' \
  --from-literal=postgres-password='<strong-password>'
```

> [!NOTE]
> - Keep `database`/`username` aligned with `postgresql.auth.{database,username}` in chart values
>   (both default to: `idegym`).
> - `host` value must match `postgresql.fullnameOverride` in the chart values (default: `postgres`)
> - The `postgres-password` key seeds the built-in `postgres` superuser inside the bundled DB
>   Skipping it logs a warning at pod start and leaves the superuser without a password.
>   You'll want it set in production for admin-level operations.
> - If you manage your own external Postgres instance (i.e., GCP CloudSQL), set `host` and `port` to
>   your DB endpoint, drop `postgres-password`, and skip the bundled chart entirely.

#### `grafana` (only when monitoring is enabled)

The Grafana subchart is configured with `admin.existingSecret: grafana`:

```shell
kubectl create secret generic grafana -n idegym \
  --from-literal=username=admin \
  --from-literal=password='<strong-password>'
```

#### `tracing` (only when tracing is enabled)

The orchestrator pulls tracing environment variables values from a secret named `tracing`.
Both lookups are `optional: true`, so this is only needed for backends that require credentials,
such as Grafana Cloud Tempo or any tenant-authenticated OTLP endpoint:

```shell
kubectl create secret generic tracing -n idegym \
  --from-literal=username='<tenant-id>' \
  --from-literal=password='<api-token>'
```

### Install the chart

Pull the subchart dependencies (only needed once, and again whenever `Chart.yaml` changes):

```shell
helm dependency update charts/idegym
```

Minimal install — orchestrator + bundled PostgreSQL only:

```shell
helm install idegym charts/idegym -n idegym \
  --set deployment.imagePullSecrets[0].name=regcred
```

Full install — adds Prometheus, Grafana, and Tempo, and sends traces to the in-cluster Tempo:

```shell
helm install idegym charts/idegym -n idegym \
  --set deployment.imagePullSecrets[0].name=regcred \
  --set prometheus.enabled=true \
  --set grafana.enabled=true \
  --set tempo.enabled=true \
  --set deployment.otel.tracing.endpoint=http://tempo:4318/v1/traces
```

Watch the rollout:

```shell
kubectl get pods -n idegym -w
```

`idegym-*` and `postgres-*` should reach `Running`/`Ready`.
With monitoring enabled, `grafana-*`, `prometheus-*`, and `tempo-*` come up as well.

### Expose the orchestrator

The chart's Service is `ClusterIP` and the Ingress is disabled by default. Two options:

1. **Port-forward** (simplest, no extra setup):

    ```shell
    kubectl port-forward svc/idegym 8000:80 -n idegym
    curl http://localhost:8000/health
    # → {"status":"healthy"}
    ```

2. **Ingress + Minikube tunnel** (closer to a production setup). Add the hostname to `/etc/hosts`:

    ```shell
    echo "127.0.0.1 idegym.test" | sudo tee -a /etc/hosts
    ```

Re-install (or `helm upgrade`) with the ingress enabled:

```shell
helm upgrade idegym charts/idegym -n idegym \
  --reuse-values \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.host=idegym.test
```

> [!NOTE]
> The Ingress template references a TLS Secret named `tls`. For a local minikube run you can either
> create a self-signed `tls` Secret or skip TLS by editing the ingress template; on a fresh install
> without that Secret, browsers will warn about the cert but the route still resolves.

In a **separate terminal window**, start the Minikube tunnel:

```shell
sudo minikube tunnel
```

> [!WARNING]
> Keep this terminal open. The tunnel must stay active for services to be reachable.

Verify:

```shell
curl -k https://idegym.test/health
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
# Rebuild and load the new image into Minikube
uv run python scripts/build_orchestrator_image.py

# Point the chart at the local image and force a re-pull from the in-cluster cache
helm upgrade idegym charts/idegym -n idegym \
  --reuse-values \
  --set deployment.image.repository=ghcr.io/jetbrains-research/idegym/orchestrator \
  --set deployment.image.tag=latest \
  --set deployment.image.pullPolicy=IfNotPresent

# Or, if image coordinates are unchanged, just bounce the rollout:
kubectl rollout restart deployment/idegym -n idegym
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

Only available if you installed with `{grafana,prometheus,tempo}.enabled` set to `true`.
Port-forward the Grafana service:

```shell
kubectl port-forward svc/grafana 3000:80 -n idegym
```

Then open <http://localhost:3000>.
Log in with the credentials you placed in the `grafana` secret earlier.

Grafana is pre-configured via a chart-rendered `ConfigMap` (provisioned at runtime by the Grafana sidecar) with the
following data sources:

- **Prometheus**: application and infrastructure metrics.
- **Tempo**: distributed traces (only useful if you also specified `--set deployment.otel.tracing.endpoint`).

---

## Cluster Cleanup

```shell
# Uninstall the chart (leaves PVCs behind by default)
helm uninstall idegym -n idegym

# Drop the namespace, including PVCs and the secrets you created
kubectl delete namespace idegym

# Stop Minikube
minikube stop

# Delete the cluster entirely (optional)
minikube delete
```

> [!NOTE]
> `helm uninstall` does not delete `PersistentVolumeClaim` resources, but the namespace deletion does
> If you want the bundled PostgreSQL data to survive a reinstall,
> skip the namespace delete and the PVC will reattach when you `helm install` again with the same release name.

---

## Troubleshooting

### Pods in `ImagePullBackOff`

The image is not available in Minikube. Check what's loaded:

```shell
minikube image ls | grep ghcr.io/jetbrains-research/idegym/orchestrator
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

### Port 5000 is already in use when starting a local Docker registry

On macOS, **AirPlay Receiver** (part of Control Center) binds to port 5000 by default, so
`docker run -p 5000:5000 registry:2` fails with *address already in use*.

You have two options:

1. **Disable AirPlay Receiver** (frees port 5000 permanently):

    > System Settings → General → AirDrop & Handoff → AirPlay Receiver → off

2. **Use a different port** (no system change required):

    ```shell
    docker run -d -p 5001:5000 --name registry registry:2
    ```

    Then pass the alternate address when running integration tests:

    ```shell
    IDEGYM_TEST_REGISTRY=localhost:5001 uv run pytest integration-tests/
    ```

### Authentication errors when pulling from GHCR

Verify the `regcred` secret exists and is referenced by the orchestrator service account:

```shell
kubectl get secret regcred -n idegym
kubectl get serviceaccount -n idegym -o yaml | grep imagePullSecrets
```
