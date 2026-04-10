# Remote Deployment Guide

This guide covers deploying IdeGYM to a production Kubernetes cluster.

## Prerequisites

### Tools

| Tool | Version | Purpose |
|------|---------|---------|
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | >= 1.28 | Kubernetes CLI |
| [Docker](https://docs.docker.com/get-docker/) | >= 24 | Build container images |
| [uv](https://github.com/astral-sh/uv) | >= 0.10.0 | Python package manager (for build scripts) |

### Cluster requirements

- Kubernetes >= 1.28
- [gVisor](https://gvisor.dev/docs/user_guide/install/) runtime class (`gvisor`) installed on worker nodes,
  if you want sandboxed environment containers
- A container registry accessible from the cluster (e.g., GHCR, Docker Hub, or a self-hosted registry)
- A `StorageClass` for PostgreSQL persistent storage
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

## Step 3: Configure Image Pull Credentials

If your images are in a private registry, create an image pull secret:

```shell
kubectl create secret docker-registry regcred \
  --docker-server=ghcr.io \
  --docker-username=<your-username> \
  --docker-password=<your-token> \
  --namespace=idegym
```

For GHCR, create a [Personal Access Token](https://github.com/settings/tokens) with the `read:packages` scope.

---

## Step 4: Configure Authentication

The orchestrator uses HTTP Basic Authentication. Create a Kubernetes secret with credentials:

```shell
kubectl create secret generic idegym-basic-auth \
  --from-literal=username=<admin-username> \
  --from-literal=password=<strong-password> \
  --namespace=idegym
```

The orchestrator deployment mounts these as `IDEGYM_AUTH_USERNAME` and `IDEGYM_AUTH_PASSWORD` environment variables.

---

## Step 5: Review and Adjust Kubernetes Manifests

All manifests are under `orchestrator/kubernetes/`. Review the following before deploying:

### `orchestrator/kubernetes/postgresql/`

- `statefulset.yaml` — adjust `storageClassName` to match your cluster's available storage class
- Consider changing the default password in the `postgresql` secret

### `orchestrator/kubernetes/orchestrator/`

- `deployment.yaml` — verify the image tag and resource requests/limits
- `ingress.yaml` — update the `host` field to your domain name

Example: update the ingress host:

```yaml
# orchestrator/kubernetes/orchestrator/ingress.yaml
spec:
  rules:
    - host: idegym.yourdomain.com   # ← update this
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: orchestrator
                port:
                  number: 80
```

### `orchestrator/kubernetes/prometheus/` and `orchestrator/kubernetes/grafana/`

Adjust retention, storage, and resource limits as needed for your workload.

---

## Step 6: Deploy

Apply everything with Kustomize:

```shell
kubectl apply -k orchestrator/kubernetes/
```

Or apply components individually:

```shell
kubectl apply -f orchestrator/kubernetes/postgresql/ -n idegym
kubectl apply -f orchestrator/kubernetes/tempo/ -n idegym
kubectl apply -f orchestrator/kubernetes/prometheus/ -n idegym
kubectl apply -f orchestrator/kubernetes/grafana/ -n idegym
kubectl apply -f orchestrator/kubernetes/orchestrator/ -n idegym
```

Wait for all pods to become ready:

```shell
kubectl rollout status deployment/orchestrator -n idegym
kubectl rollout status statefulset/postgresql -n idegym
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
kubectl logs -l app=orchestrator -n idegym --follow
```

---

## Step 8: Configure the Orchestrator for Kaniko Builds

The orchestrator builds environment images inside the cluster using
[Kaniko](https://github.com/GoogleContainerTools/kaniko). Configure it to use your registry:

Set these environment variables in the orchestrator deployment:

| Variable | Description | Example |
|---|---|---|
| `DOCKER_REGISTRY` | Registry where Kaniko pushes built images | `ghcr.io/jetbrains-research/idegym` |
| `KANIKO_INSECURE_REGISTRY` | Set to `"true"` for HTTP registries | `"false"` |

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

## Updating the Orchestrator

To deploy a new version of the orchestrator:

```shell
# Build and push the new image
uv run python scripts/build_orchestrator_image.py --push

# Roll out the update
kubectl rollout restart deployment/orchestrator -n idegym

# Watch the rollout
kubectl rollout status deployment/orchestrator -n idegym
```

---

## Accessing Observability Tools

### Grafana

Port-forward from your local machine:

```shell
kubectl port-forward svc/grafana 3000:3000 -n idegym
```

Open [http://localhost:3000](http://localhost:3000) (default credentials: `admin` / `changeme`).

For production, expose Grafana via ingress and change the default password.

### Prometheus

```shell
kubectl port-forward svc/prometheus 9090:9090 -n idegym
```

Open [http://localhost:9090](http://localhost:9090).

---

## Database Management

### Migrations

The orchestrator runs Alembic migrations automatically on startup. To run them manually:

```shell
kubectl exec -it deployment/orchestrator -n idegym -- \
  uv run alembic upgrade head
```

### Connecting to PostgreSQL

```shell
kubectl exec -it statefulset/postgresql -n idegym -- \
  psql -U postgres -d idegym
```

---

## Connecting to the Orchestrator from Code

The orchestrator uses HTTP Basic Authentication. Use the `idegym` client library:

```python
from idegym.client.client import IdeGYMClient

client = IdeGYMClient(
    base_url="https://idegym.yourdomain.com",
    username="admin",
    password="your-password",
)
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
- [ ] `regcred` image pull secret created in the `idegym` namespace
- [ ] `idegym-basic-auth` secret created with strong credentials
- [ ] PostgreSQL password rotated from the default
- [ ] Ingress hostname configured and DNS record pointing to your cluster
- [ ] TLS certificate configured for the ingress (recommended: [cert-manager](https://cert-manager.io))
- [ ] Resource limits reviewed for your expected workload
- [ ] Grafana default password changed
- [ ] Backup strategy for PostgreSQL persistent volume
- [ ] gVisor runtime class available if using sandboxed containers
