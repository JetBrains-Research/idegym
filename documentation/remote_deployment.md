# Remote Deployment Guide

This guide covers deploying IdeGYM to a production Kubernetes cluster.

## Prerequisites

### Tools

| Tool                                               | Version   | Purpose                                       |
|----------------------------------------------------|-----------|-----------------------------------------------|
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | >= 1.28   | Kubernetes CLI                                |
| [Helm](https://helm.sh/docs/intro/install/)        | >= 3.19   | Install the bundled chart at `charts/idegym/` |
| [Docker](https://docs.docker.com/get-docker/)      | >= 24     | Build container images                        |
| [uv](https://github.com/astral-sh/uv)              | >= 0.10.0 | Python package manager (for build scripts)    |

### Cluster requirements

- Kubernetes >= 1.28
- [gVisor](https://gvisor.dev/docs/user_guide/install/) runtime class (`gvisor`) installed on worker nodes,
  if you want sandboxed environment containers
- A container registry accessible from the cluster (e.g., GHCR, Docker Hub, or a self-hosted registry)
- A default `StorageClass` for PostgreSQL persistent storage
  (i.e., annotated with `storageclass.kubernetes.io/is-default-class: "true"`)
- An ingress controller (e.g., [ingress-nginx](https://kubernetes.github.io/ingress-nginx/))
- `kubectl` configured to point at your cluster (`kubectl get nodes` should succeed)

---

## Step 1: Build and Push Images

### Build the base server image

The base server image is the foundation for all environment containers. Build it for both architectures:

```shell
uv run python scripts/build_server_images.py --push --multiplatform
```

This builds from `Dockerfile.jinja` and pushes to:
```
ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest
```

To use your own registry, set the `DOCKER_REGISTRY` environment variable before running:

```shell
DOCKER_REGISTRY=your-registry.example.com/idegym \
  uv run python scripts/build_server_images.py --push --multiplatform
```

### Build the orchestrator image

```shell
uv run python scripts/build_orchestrator_image.py --push --multiplatform
```

Alternatively, build with Docker directly:

```shell
docker build \
  -f orchestrator/Dockerfile \
  -t ghcr.io/jetbrains-research/idegym/orchestrator:latest \
  --platform linux/amd64,linux/arm64 \
  --push \
  .
```

---

## Step 2: Configure the Namespace

Create a dedicated namespace:

```shell
kubectl create namespace idegym
```

---

## Step 3: Configure Image Pull Credentials (optional)

The orchestrator and bundled server images are public on GHCR, so the installation itself needs no pull credentials.
Two cases require credentials, and they are independent:

1. **Environment and Kaniko pods that target a private registry**. The orchestrator looks up a secret named `regcred`
   by that exact name and mounts it into the pods it creates. Provision it in the release namespace:

   ```shell
   kubectl create secret docker-registry regcred \
     --docker-server=ghcr.io \
     --docker-username=username \
     --docker-password=ghp_... \
     --namespace=idegym
   ```

   For GHCR, create a [Personal Access Token](https://github.com/settings/tokens) with the `read:packages` scope.

2. **A custom orchestrator image in a private registry**. Set `deployment.imagePullSecrets` on the chart
   to reference an image pull secret in the release namespace (see [Step 5](#step-5-configure-chart-values)).

---

## Step 4: Configure Application Secrets

The bundled PostgreSQL and Grafana subcharts manage their own credentials by default.
The orchestrator reads the database password from PostgreSQL's auto-generated secret.
Override paths are provided when you want to source credentials from elsewhere.

### PostgreSQL credentials

By default, the Bitnami PostgreSQL subchart creates a `postgres` secret with `password` and `postgres-password` keys,
and the orchestrator consumes them automatically. The database name and application user default to `idegym`
and are configured via:

```yaml
postgresql:
  auth:
    database: idegym
    username: idegym
```

To source the orchestrator's connection details from an existing secret (e.g., from External Secrets
Operator, a cloud secrets manager, or a self-managed Postgres instance), override the relevant fields
under `deployment.database`:

```yaml
deployment:
  database:
    password:
      valueFrom:
        secretKeyRef:
          name: # Name of your database credentials secret
          key: # Data key corresponding to the password
    # Repeat for host, port, name, username
```

Each field also accepts a primitive (e.g., `port: 5432`) if you want it set inline rather than sourced from a `Secret`.
Unset fields fall back to the bundled subchart's values. When working with an external database,
you should disable the bundled chart:

```yaml
postgresql:
  enabled: false
```

### Grafana credentials (only when `grafana.enabled=true`)

The Grafana subchart generates a random admin password on the first installation and preserves it across upgrades.
Retrieve it with:

```shell
kubectl get secret grafana -n idegym -o jsonpath='{.data.admin-password}' | base64 -d
```

To use your own credentials, create a secret with `admin-user` and `admin-password` keys and reference it:

```yaml
grafana:
  admin:
    existingSecret: # Name of your Grafana secret
```

### `tracing` (only when `deployment.otel.tracing.endpoint` is set and the backend requires auth)

The orchestrator pulls tracing environment variables values from a secret named `tracing`.
Both lookups are `optional: true`, so this is only needed for backends that require credentials,
such as Grafana Cloud Tempo or any tenant-authenticated OTLP endpoint:

```shell
kubectl create secret generic tracing -n idegym \
  --from-literal=username='<tenant-id>' \
  --from-literal=password='<api-token>'
```

---

## Step 5: Configure Chart Values

The chart ships sensible defaults.
Override what your environment requires in a local `values.yaml` overlay
(committed alongside your deployment automation, never with secrets in it).
Here is an example that configures custom image pull secrets, an OLTP tracing endpoint
and creates an `Ingress`:

```yaml
deployment:
  imagePullSecrets:
    - name: regcred
  otel:
    tracing:
      endpoint: https://tempo.yourdomain.com/v1/traces

ingress:
  enabled: true
  className: nginx
  host: idegym.yourdomain.com
  tls:
    enabled: true
```

When `ingress.tls.enabled=true`, the `Ingress` template references a TLS secret in the release namespace.
By default, the name is derived from the release, but can be overridden with `ingress.tls.secretName`.
Provision the secret with [cert-manager](https://cert-manager.io) or pre-create it manually.

---

## Step 6: Deploy

Pull subchart dependencies and install:

```shell
helm dependency update charts/idegym
helm install idegym charts/idegym -n idegym -f values.yaml
```

Wait for the rollout:

```shell
kubectl rollout status deployment/idegym -n idegym
kubectl rollout status statefulset/postgres -n idegym
```

---

## Step 7: Verify the Deployment

Check the orchestrator health endpoint:

```shell
curl https://idegym.yourdomain.com/health
# → {"status":"healthy"}
```

Check pod status:

```shell
kubectl get pods -n idegym
```

View orchestrator logs:

```shell
kubectl logs deployments/idegym -n idegym --follow
```

---

## Step 8: Configure the Orchestrator for Kaniko Builds

The orchestrator builds environment images inside the cluster using
[Kaniko](https://github.com/GoogleContainerTools/kaniko). Configure it to use your registry:

Set these environment variables in the orchestrator deployment:

| Variable                   | Description                               | Example                             |
|----------------------------|-------------------------------------------|-------------------------------------|
| `DOCKER_REGISTRY`          | Registry where Kaniko pushes built images | `ghcr.io/jetbrains-research/idegym` |
| `KANIKO_INSECURE_REGISTRY` | Set to `"true"` for HTTP registries       | `"false"`                           |

For a production setup with GHCR, Kaniko also needs credentials. Mount a Docker config as a secret:

```shell
# Create a Docker config JSON for Kaniko
kubectl create secret generic kaniko-registry-creds \
  --from-file=config.json=/path/to/docker-config.json \
  --namespace=idegym
```

The Docker config should contain credentials for your registry:

```json
{
  "auths": {
    "ghcr.io": {
      "auth": "<base64-encoded-username:token>"
    }
  }
}
```

---

## Step 9: Dedicated Node Pools (Optional)

You can isolate IdeGYM workloads onto dedicated nodes using a tainted node pool.
When enabled, pods prefer dedicated nodes but fall back to regular ones if capacity is unavailable.

### Create the node pool

For Google Cloud, this can be done with `gcloud` CLI:

```shell
gcloud container node-pools create idegym \
    --node-labels=jetbrains.com/idegym=true \
    --node-taints=jetbrains.com/idegym=:NoSchedule \
    # Set other required options as necessary:
    # `cluster`, `machine-type`, `num-nodes`, `min-nodes`, ...
```

### Enable the feature

Set these environment variables on the orchestrator deployment:

| Variable                             | Description                                              | Default                |
|--------------------------------------|----------------------------------------------------------|------------------------|
| `IDEGYM_NODE_POOL_ENABLED`           | Enable dedicated node pool scheduling                    | `False`                |
| `IDEGYM_NODE_POOL_TAINT_KEY`         | Taint/label key on dedicated nodes                       | `jetbrains.com/idegym` |
| `IDEGYM_NODE_POOL_PREFERENCE_WEIGHT` | Scheduling weight (1-100) for preferring dedicated nodes | `100`                  |

When enabled, all dynamically created pods (sandbox servers, Kaniko image builds, and node holders)
receive a preferred node affinity and toleration matching the configured key.

---

## Step 10: Pod Snapshots (GKE only, Optional)

IdeGYM can checkpoint a running server pod and restore future pods from that snapshot, skipping cold-start work.
See [Orchestrator → Pod Snapshots](../orchestrator/README.md#pod-snapshots-checkpointrestore) for the API surface
and how the snapshot/restore flow is modelled. This section covers what you need on the cluster side.

The feature is built on [GKE pod snapshots](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots)
and is therefore **GKE-only**. The snapshot-related resources rendered by the chart live under
[`charts/idegym/templates/podsnapshot*.yaml`](../charts/idegym/templates) and are all gated on the
`podsnapshot.gke.io/v1` API being available.

### Prerequisites

Follow the [GKE pod snapshots guide](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots)
end-to-end — it covers the cluster-side prerequisites. Before continuing, confirm
you have:

- [Pod snapshots enabled on the cluster](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots#enable).
- [A GCS bucket created for the checkpoints](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots#create-bucket).
- [The GKE pod-snapshot controller (`service-<project-number>@container-engine-robot.iam.gserviceaccount.com`)
  granted the role required by the GKE guide on that bucket](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots#grant-controller-permissions).
- [The Kubernetes ServiceAccount the chart will create for snapshot-enabled pods
  granted the IAM permissions the GKE guide requires on the bucket](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/pod-snapshots#grant-permissions).

### Enable the feature

Set `podSnapshot.enabled=true` and point the chart at your bucket. The shape under `podSnapshot.snapshotStorageConfig`,
`podSnapshot.retentionConfig`, and `podSnapshot.snapshotGroupingRules` mirrors the GKE
[`PodSnapshotPolicy`](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots) /
`PodSnapshotStorageConfig` CRD spec verbatim, so all GKE-documented fields work without translation.

Minimal example — overlay only what differs from the chart defaults in
[`charts/idegym/values.yaml`](../charts/idegym/values.yaml):

```yaml
podSnapshot:
  enabled: true
  snapshotStorageConfig:
    gcs:
      bucket: my-idegym-snapshots
```

The chart then renders the `PodSnapshotPolicy`, the `PodSnapshotStorageConfig`, and a Kubernetes `ServiceAccount`
for snapshot-enabled pods.

It also wires `IDEGYM_POD_SNAPSHOT_ENABLED=True` and `IDEGYM_POD_SNAPSHOT_SERVICE_ACCOUNT_NAME=<KSA>` on the
orchestrator deployment — the first gates the snapshot endpoints, the second is the KSA the orchestrator attaches
to every sandbox pod it creates.

> **Notes.** For a snapshot to actually happen and be restorable: the target pod must not run on a GCP E2 node,
> must use the `gvisor` runtime, and on restore the server configuration must match the snapshotted one. For best
> latency, keep the GCS bucket in the same region as the cluster.

### Verify

```shell
kubectl get podsnapshotpolicy,podsnapshotstorageconfig -n idegym
```

Snapshot a running server via the client and confirm it shows up in the bucket:

```python
response = await client.snapshot_server(server_id=...)
print(response.snapshot_id)
```

---

## Updating the Orchestrator

To deploy a new version of the orchestrator:

```shell
# Build and push the new image
uv run python scripts/build_orchestrator_image.py --push

# Bump Chart.yaml appVersion (or override --set deployment.image.tag) and upgrade
helm upgrade idegym charts/idegym -n idegym -f values.yaml

# Watch the rollout
kubectl rollout status deployment/idegym -n idegym
```

If the image tag itself didn't change, but you want to pull a freshly built image with the same tag,
either `helm upgrade` (which restarts pods) or:

```shell
kubectl rollout restart deployment/idegym -n idegym
```

---

## Accessing Observability Tools

### Grafana

Only deployed if you installed with `--set grafana.enabled=true`. Port-forward from your local machine:

```shell
kubectl port-forward svc/grafana 3000:80 -n idegym
```

Open <http://localhost:3000> and log in as `admin`. Retrieve the password from the chart-managed secret:

```shell
kubectl get secret grafana -n idegym -o jsonpath='{.data.admin-password}' | base64 -d
```

For production, expose Grafana via its own Ingress rather than port-forwarding.

### Prometheus

Only deployed if you installed with `--set prometheus.enabled=true`.

```shell
kubectl port-forward svc/prometheus 9090:9090 -n idegym
```

Open <http://localhost:9090>.

---

## Database Management

### Migrations

The orchestrator runs Alembic migrations automatically on startup. To run them manually:

```shell
kubectl exec -it deployment/idegym -n idegym -- \
  uv run alembic upgrade head
```

### Connecting to PostgreSQL

```shell
kubectl exec -it postgres-0 -n idegym -- \
  psql -U idegym -d idegym
```

You'll be prompted for the application user's password. Either retrieve it from the chart-managed secret:

```shell
kubectl get secret postgres -n idegym -o jsonpath='{.data.password}' | base64 -d
```

Or from wherever it's stored.

---

## Connecting to the Orchestrator from Code

The orchestrator uses HTTP Basic Authentication. Use the `idegym` client library:

```python
import asyncio
from idegym.client.client import IdeGYMClient
from idegym.api.auth import BasicAuth

async def main():
    async with IdeGYMClient(
        orchestrator_url="https://idegym.yourdomain.com",
        name="my-client",
        namespace="idegym",
        auth=BasicAuth(username="admin", password="your-password"),
    ) as client:
        response = await client.health_check()
        print(response.status)  # → "healthy"

asyncio.run(main())
```

Or make raw HTTP requests with a `Basic` `Authorization` header:

```python
import base64
import httpx

credentials = base64.b64encode(b"admin:your-password").decode()
headers = {"Authorization": f"Basic {credentials}"}

response = httpx.get("https://idegym.yourdomain.com/health", headers=headers)
```

---

## Production Checklist

- [ ] Images pushed to a registry accessible from the cluster
- [ ] `regcred` image pull secret created if environment or Kaniko pods target a private registry
- [ ] `deployment.imagePullSecrets` set if the orchestrator image is in a private registry
- [ ] PostgreSQL credentials reviewed (either auto-generate with chart or override via `deployment.database` for external connections)
- [ ] Grafana admin credentials reviewed if `grafana.enabled=true` (either auto-generate with chart or override via values)
- [ ] `tracing` secret created if your OTLP backend requires authentication
- [ ] TLS secret provisioned for the orchestrator `Ingress` if enabled
      (preferrably through [cert-manager](https://cert-manager.io))
- [ ] Ingress hostname (`ingress.host`) configured and DNS record pointing to your cluster
- [ ] `deployment.resources` populated with appropriate requests/limits for your workload
- [ ] Backup strategy for PostgreSQL persistent volume
- [ ] gVisor runtime class available on nodes if using sandboxed containers
- [ ] If using pod snapshots: GKE pod-snapshot feature enabled on the cluster, GCS bucket provisioned, snapshot KSA granted the required IAM on the bucket, `podSnapshot.enabled=true` and `snapshotStorageConfig.gcs.bucket` set
