# Local Deployment Guide

This guide explains how to run the full IdeGYM stack locally on macOS or Linux using Minikube.

The build and `helm`/`kubectl` commands are identical on both platforms. Where the two differ — mostly
around exposing the orchestrator over the network — the guide calls out **macOS** and **Linux** explicitly.
Install commands assume [Homebrew](https://brew.sh) on macOS and your distribution's package manager on Linux.

Two approaches are covered:

1. **[Using pre-built images from GHCR](#approach-1-pre-built-ghcr-images)** — the fastest way to get started; pulls
   ready-made orchestrator and server images from GitHub Container Registry.
2. **[Building images locally](#approach-2-building-images-locally)** — builds all images from source and loads them
   directly into Minikube, with no dependency on an external registry. This mirrors what the e2e test suite does.

---

## Prerequisites

Install the required tools.

### Docker

**macOS:**

```shell
brew install --cask docker-desktop
# or the CLI-only formula
brew install docker
```

Start Docker Desktop and wait for it to be ready.

**Linux:** install [Docker Engine](https://docs.docker.com/engine/install/) via your distribution's
package manager (e.g. the `docker.io` / `docker-ce` packages) and make sure your user is in the `docker`
group so the daemon is reachable without `sudo` (`sudo usermod -aG docker "$USER"`, then re-login).

### Kubernetes tools

**macOS:**

```shell
brew install helm kubernetes-cli minikube
```

**Linux:** install [`helm`](https://helm.sh/docs/intro/install/),
[`kubectl`](https://kubernetes.io/docs/tasks/tools/), and
[`minikube`](https://minikube.sigs.k8s.io/docs/start/) from their official docs or your package manager.

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

Approach 1 installs into the `idegym` namespace. Approach 2 (local builds) uses a separate
`idegym-local` namespace so the two installations never collide. Create the one for the approach
you're following:

```shell
# Approach 1 (pre-built GHCR images)
kubectl create namespace idegym

# Approach 2 (local image builds)
kubectl create namespace idegym-local
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

### Configure Docker registry access (optional)

The orchestrator and bundled server images are public on GHCR, so a minimal installation needs no pull credentials.
You only need a dedicated `Secret` when your client spawns environments whose images live in a private registry.
The orchestrator looks the secret up by the fixed name `regcred` and mounts it into environment and Kaniko pods.
Create a [GitHub PAT](https://github.com/settings/tokens) with the `read:packages` scope and register it as a
Kubernetes secret:

```shell
kubectl create secret docker-registry regcred \
  --docker-server=ghcr.io \
  --docker-username=username \
  --docker-password=ghp_... \
  --namespace=idegym
```

> [!TIP]
> Verify the secret was created:
>
> ```shell
> kubectl get secrets -n idegym
> ```

Separately, `deployment.imagePullSecrets` on the chart controls credentials for pulling the orchestrator image itself.
Set it if you've built a custom orchestrator image and pushed it to a private registry.

### Create the application secrets

By default, the bundled PostgreSQL and Grafana subcharts each provision their own credentials Secret,
and the orchestrator reads the database password from PostgreSQL's. The only Secret below is needed
when you opt into a tracing backend that requires credentials.

> [!WARNING]
> The bundled PostgreSQL subchart **auto-generates the database password on first install** and stores it
> in the `postgres` Secret. That password is baked into the PostgreSQL data directory (the PVC) on first boot.
> If you later reinstall the release while the old PVC is still around, the subchart mints a *new* password in
> the Secret that no longer matches the one persisted in the PVC, and the orchestrator can't authenticate.
> When you tear a local deployment down, delete the namespace (or the `data-postgres-0` PVC) so PostgreSQL and
> the Secret are regenerated together. See [Cluster Cleanup](#cluster-cleanup) and, for stable long-lived
> deployments, [Stabilizing the PostgreSQL password](remote_deployment.md#stabilizing-the-postgresql-password).

> [!TIP]
> Grafana's admin password is randomly generated on initial installation and preserved across upgrades.
> Retrieve it with:
>
> ```shell
> kubectl get secret grafana -n idegym -o jsonpath='{.data.admin-password}' | base64 -d
> ```
>
> To use your own credentials, override `grafana.admin.existingSecret` with the name of a Secret containing
> `admin-user` and `admin-password` keys.

> [!TIP]
> To point the services at an existing `Secret`, override each connection field under `database`.

#### `tracing` (required when tracing is enabled and behind auth)

The orchestrator pulls tracing environment variables values from a secret named `tracing`.
Both lookups are `optional`, so this is only needed for backends that require credentials,
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

> [!IMPORTANT]
> The chart in this repository ships with an empty `appVersion`, so it does **not** default the image tag —
> you must pin `deployment.image.tag` and `watcher.image.tag` to a released version, otherwise the pods
> render an empty tag (`orchestrator:`) and land in `ImagePullBackOff`. Pick a published version from the
> [GHCR packages page](https://github.com/orgs/JetBrains-Research/packages?ecosystem=container)
> and substitute it for `<version>` below. (When you install the *published* chart from the OCI registry
> instead of this repo copy, its `appVersion` is set and the tags default correctly.)

For a minimal installation, only the orchestrator and PostgreSQL instance are created:

```shell
helm install idegym charts/idegym -n idegym \
  --set deployment.image.tag=<version> \
  --set watcher.image.tag=<version>
```

A full installation also adds Prometheus, Grafana, and Tempo:

```shell
helm install idegym charts/idegym -n idegym \
  --set deployment.image.tag=<version> \
  --set watcher.image.tag=<version> \
  --set prometheus.enabled=true \
  --set grafana.enabled=true \
  --set tempo.enabled=true \
  --set deployment.otel.tracing.endpoint=http://tempo:4318/v1/traces
```

Watch the rollout:

```shell
kubectl get pods -n idegym -w
```

`idegym-*`, `idegym-watcher-*`, and `postgres-*` should reach `Running`/`Ready`.
With monitoring enabled, `grafana-*`, `prometheus-*`, and `tempo-*` come up as well.

### Expose the orchestrator

The chart's Service is `ClusterIP` and the Ingress is disabled by default. Two options:

#### Option 1: Port-forward (simplest, identical on macOS and Linux)

This is the recommended path for local work — it needs no ingress, no tunnel, and no `/etc/hosts` edits,
and behaves the same on both platforms:

```shell
kubectl port-forward svc/idegym 8000:80 -n idegym
curl http://localhost:8000/health
# → {"status":"healthy"}
```

#### Option 2: Ingress + Minikube tunnel (closer to a production setup)

Re-install (or `helm upgrade`) with the ingress enabled:

```shell
helm upgrade idegym charts/idegym -n idegym \
  --reuse-values \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.host=idegym.test
```

In a **separate terminal window**, start the Minikube tunnel and keep it running:

```shell
minikube tunnel
```

> [!WARNING]
> Keep this terminal open. The tunnel must stay active for services to be reachable.

The tunnel exposes the ingress controller under a `LoadBalancer` external IP, but **that IP differs by
platform**, and so does the `/etc/hosts` entry you need:

- **macOS** (Docker driver): the Docker network isn't routable from the host, so `minikube tunnel` maps the
  external IP onto `127.0.0.1`. Point the hostname there, and run the tunnel with `sudo` if it asks for
  privileges to bind ports 80/443:

    ```shell
    echo "127.0.0.1 idegym.test" | sudo tee -a /etc/hosts
    ```

- **Linux** (Docker driver): the Docker bridge *is* routable from the host, so the tunnel assigns a real
  cluster-range external IP (e.g. `10.x.x.x`) — **not** `127.0.0.1`. Do **not** run `sudo minikube tunnel`:
  as root it can't see your user's Minikube profile and aborts with
  `MK_USAGE_NO_PROFILE: No minikube profile was found`. Run it as your normal user, read the assigned
  external IP, and point `/etc/hosts` at *that* IP:

    ```shell
    IP=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
    echo "$IP idegym.test" | sudo tee -a /etc/hosts
    ```

    On Linux, plain `kubectl port-forward` (Option 1) is usually the least fuss.

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

You need three images loaded into Minikube: the base server image, the orchestrator, and the watcher
(the watcher is enabled by default — `watcher.enabled: true` — so skipping it leaves that pod in
`ImagePullBackOff`).

> [!NOTE]
> All three build scripts default to a **native single-platform build** (your host's architecture), which
> uses the plain `docker` driver — no `buildx` container driver, QEMU emulation, or `arm64` cross-build.
> On a Linux/amd64 host that is exactly what Minikube needs. Pass `--multiplatform` only when you deliberately
> want a `linux/amd64` + `linux/arm64` image (e.g. building on Apple Silicon for an amd64 cluster).

**Base server image** (Debian bookworm, used as the base for environment containers):

```shell
uv run python scripts/build_server_images.py --skip-base ubuntu
```

This builds the image from the server template and tags it as
`ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest`.
Then load it into Minikube:

```shell
minikube image load ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest
```

**Orchestrator image**:

```shell
uv run python scripts/build_orchestrator_image.py
```

**Watcher image**:

```shell
uv run python scripts/build_watcher_image.py
```

The orchestrator and watcher scripts build and automatically load their `:latest` image into Minikube.

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

### Install the chart

Pull the subchart dependencies (only needed once, and again whenever `Chart.yaml` changes):

```shell
helm dependency update charts/idegym
```

Install into the `idegym-local` namespace, pinning both images to the `:latest` tag the build scripts
produced. The tag override is required because the repo chart ships an empty `appVersion` (see the
[!IMPORTANT] note under [Approach 1 → Install the chart](#install-the-chart)):

```shell
helm install idegym charts/idegym -n idegym-local \
  --create-namespace \
  --set deployment.image.tag=latest \
  --set watcher.image.tag=latest
```

> [!TIP]
> If you created the namespace earlier you can drop `--create-namespace`; it's a no-op when the namespace
> already exists. If you upgrade an existing release instead, keep the tags pinned — a `--reuse-values`
> upgrade that omits them re-renders an empty tag:
>
> ```shell
> helm upgrade idegym charts/idegym -n idegym-local \
>   --reuse-values \
>   --set deployment.image.tag=latest \
>   --set watcher.image.tag=latest
> ```

Watch the rollout until `idegym-*`, `idegym-watcher-*`, and `postgres-*` are `Running`/`Ready`:

```shell
kubectl get pods -n idegym-local -w
```

### Configure host access

The tunnel semantics are the same as in
[Approach 1 → Expose the orchestrator](#option-2-ingress--minikube-tunnel-closer-to-a-production-setup) —
only the hostname (`idegym-local.test`) and namespace (`idegym-local`) differ. Enable the ingress:

```shell
helm upgrade idegym charts/idegym -n idegym-local \
  --reuse-values \
  --set deployment.image.tag=latest \
  --set watcher.image.tag=latest \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.host=idegym-local.test
```

In a **separate terminal**, start the tunnel and keep it running:

```shell
minikube tunnel
```

Then add the `/etc/hosts` entry for the platform you're on:

- **macOS:** `echo "127.0.0.1 idegym-local.test" | sudo tee -a /etc/hosts`
- **Linux:** point it at the tunnel's assigned external IP, not `127.0.0.1`, and don't run the tunnel under
  `sudo` (it aborts with `MK_USAGE_NO_PROFILE`):

    ```shell
    IP=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
    echo "$IP idegym-local.test" | sudo tee -a /etc/hosts
    ```

Verify:

```shell
curl -k https://idegym-local.test/health
# → {"status":"healthy"}
```

Prefer no tunnel at all? `kubectl port-forward svc/idegym 8000:80 -n idegym-local` works the same on both
platforms.

---

## Deploying Changes to the Orchestrator

When you modify orchestrator code and want to test it in the cluster (use the namespace for your approach —
`idegym` for Approach 1, `idegym-local` for Approach 2):

```shell
# Rebuild and load the new image into Minikube
uv run python scripts/build_orchestrator_image.py

# Point the chart at the local image and force a re-pull from the in-cluster cache
helm upgrade idegym charts/idegym -n idegym-local \
  --reuse-values \
  --set deployment.image.repository=ghcr.io/jetbrains-research/idegym/orchestrator \
  --set deployment.image.tag=latest \
  --set deployment.image.pullPolicy=IfNotPresent

# Or, if image coordinates are unchanged, just bounce the rollout:
kubectl rollout restart deployment/idegym -n idegym-local
```

The build script also accepts flags:
- `--push` — push the built image to the remote registry
- `--no-cache` — disable Docker layer cache
- `--multiplatform` — build for `linux/amd64` and `linux/arm64`

> [!NOTE]
> The build is single-platform (native) by default, so a Linux/amd64 host needs no `buildx` container
> driver or QEMU. Reach for `--multiplatform` only when your machine architecture differs from the cluster
> (e.g., Apple Silicon building for amd64). See
> [Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/).

---

## Accessing Metrics and Traces

Only available if you installed with `{grafana,prometheus,tempo}.enabled` set to `true`.
Port-forward the Grafana service:

```shell
kubectl port-forward svc/grafana 3000:80 -n idegym
```

Then open <http://localhost:3000>. Log in as `admin` with the password from the chart-managed secret:

```shell
kubectl get secret grafana -n idegym -o jsonpath='{.data.admin-password}' | base64 -d
```

Grafana is pre-configured via a chart-rendered `ConfigMap` (provisioned at runtime by the Grafana sidecar) with the
following data sources:

- **Prometheus**: application and infrastructure metrics.
- **Tempo**: distributed traces (only useful if you also specified `--set deployment.otel.tracing.endpoint`).

---

## Cluster Cleanup

Use the namespace for the approach you followed (`idegym` or `idegym-local`):

```shell
# Uninstall the chart (leaves PVCs behind by default)
helm uninstall idegym -n idegym-local

# Drop the namespace, including PVCs and the secrets you created
kubectl delete namespace idegym-local

# Stop Minikube
minikube stop

# Delete the cluster entirely (optional)
minikube delete
```

> [!NOTE]
> `helm uninstall` does not delete `PersistentVolumeClaim` resources, but the namespace deletion does.
> If you want the bundled PostgreSQL data to survive a reinstall,
> skip the namespace delete and the PVC will reattach when you `helm install` again with the same release name.

> [!WARNING]
> Do **not** `helm uninstall` and then reinstall while keeping the old PostgreSQL PVC. The subchart
> auto-generates a *fresh* password into the `postgres` Secret on reinstall, but the PVC still holds the
> *original* password, so the orchestrator fails to authenticate (`password authentication failed`).
> Either delete the PVC (`kubectl delete pvc data-postgres-0 -n idegym-local`) together with the release,
> or pin the password so it survives — see
> [Stabilizing the PostgreSQL password](remote_deployment.md#stabilizing-the-postgresql-password).

---

## Troubleshooting

### Pods in `ImagePullBackOff`

First check the exact image reference the pod is trying to pull:

```shell
kubectl get pod <pod> -n idegym-local -o jsonpath='{.spec.containers[*].image}'
```

- **Tag ends in a bare colon** (e.g. `.../orchestrator:`) — you forgot to pin the tag. The repo chart has an
  empty `appVersion`, so you must pass `--set deployment.image.tag=... --set watcher.image.tag=...`
  (`latest` for local builds). See [Install the chart](#install-the-chart).
- **Tag is present but the image isn't loaded** — check what's in Minikube and reload if missing
  (this is also the usual cause for the `watcher` pod if you skipped its build step):

    ```shell
    minikube image ls | grep ghcr.io/jetbrains-research/idegym
    ```

  Then rebuild and reload as described in [Build and load images](#build-and-load-images).

### Environment-server pods `CrashLoopBackOff` (exit 137) right after they start

An env-server pod that the orchestrator spawns comes up and immediately dies. Its logs show a pydantic
validation error on `IDEGYM_OTEL_TRACING_ENDPOINT` (an empty string is not a valid URL). This happens when
the orchestrator was deployed with tracing *partially* configured — e.g. an empty
`deployment.otel.tracing.endpoint`. Either leave tracing unset entirely (recent orchestrator images then
forward nothing), or set a real endpoint:

```shell
helm upgrade idegym charts/idegym -n idegym-local \
  --reuse-values \
  --set deployment.image.tag=latest \
  --set watcher.image.tag=latest \
  --set deployment.otel.tracing.endpoint=http://tempo:4318/v1/traces
```

If you're running an older orchestrator image, rebuild and reload it
([Deploying Changes to the Orchestrator](#deploying-changes-to-the-orchestrator)) to pick up the fix that
stops forwarding an empty endpoint.

### `curl idegym.test/health` (or `idegym-local.test`) times out

1. Confirm the tunnel is running (as your normal user, **not** under `sudo` on Linux):

   ```shell
   ps aux | grep "minikube tunnel"
   ```

2. Confirm the ingress controller has an external IP, and note what it is:

   ```shell
   kubectl get svc -n ingress-nginx ingress-nginx-controller
   # macOS: EXTERNAL-IP is 127.0.0.1
   # Linux: EXTERNAL-IP is a cluster-range address (e.g. 10.x.x.x)
   ```

3. Confirm your `/etc/hosts` entry points at that EXTERNAL-IP. On Linux this is a real cluster IP, so a
   line pinning the hostname to `127.0.0.1` is wrong and the request never reaches the ingress.

4. Confirm the orchestrator pod is running:

   ```shell
   kubectl get pods -n idegym-local
   ```

When in doubt, skip the tunnel entirely and use `kubectl port-forward svc/idegym 8000:80 -n idegym-local`.

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

For environment and Kaniko pods that target a private registry, verify the `regcred` secret exists:

```shell
kubectl get secret regcred -n idegym
```

For the orchestrator pod itself, confirm `deployment.imagePullSecrets` was set if its image is private:

```shell
kubectl get deployment idegym -n idegym -o yaml | grep -A2 imagePullSecrets
```
