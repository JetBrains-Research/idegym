# E2E Tests

End-to-end tests for IdeGYM running on a local Minikube cluster.
The tests cover the full stack: image building (via Kaniko and local Docker),
server lifecycle, request forwarding, and the WebSocket protocol.

For the broader deployment context see [Local Deployment](../documentation/local_deployment.md).

## Prerequisites

### 1. Install required tools

```bash
# macOS with Homebrew
brew install --cask docker-desktop
# or
brew install docker

brew install kubernetes-cli minikube uv
```

Verify:

```bash
docker version
kubectl version --client
minikube version
uv --version
```

### 2. Install project dependencies

From the repository root:

```bash
uv python install
uv venv --seed
uv sync --all-packages --all-extras --all-groups
```

### 3. Start the Minikube cluster

```bash
minikube start \
  --addons=gvisor,ingress,registry \
  --container-runtime=containerd \
  --docker-opt containerd=/var/run/containerd/containerd.sock \
  --kubernetes-version=v1.35.0
```

The `registry` addon creates a cluster-internal Docker registry at
`registry.kube-system.svc.cluster.local`. Kaniko uses this registry to push built images.

Verify the registry is running:

```bash
minikube addons list | grep registry
kubectl run curl --rm -it --image=curlimages/curl --restart=Never -- \
  curl http://registry.kube-system.svc.cluster.local/v2/
```

### 4. Configure host access

Add the test hostname to `/etc/hosts` (only needs to be done once):

```bash
echo "127.0.0.1 idegym-local.test" | sudo tee -a /etc/hosts
```

### 5. Start the Minikube tunnel

In a **separate terminal window** (keep it open while running tests):

```bash
sudo minikube tunnel
```

---

## Running Tests

### Run all e2e tests

```bash
uv run pytest -m e2e
```

This will automatically:
1. Build the orchestrator and base server images
2. Load images into Minikube
3. Deploy all Kubernetes resources
4. Wait for services to become ready
5. Run all tests
6. Clean up resources

### Useful flags

```bash
# Skip image building (use already-loaded images)
uv run pytest -m e2e --skip-build

# Reuse existing Kubernetes resources (skip kubectl apply)
uv run pytest -m e2e --reuse-resources

# Both flags together — fastest iteration when nothing changed
uv run pytest -m e2e --skip-build --reuse-resources

# Run a specific test by keyword
uv run pytest -m e2e -k health
uv run pytest -m e2e -k "python_api"

# Run a single test file
uv run pytest -m e2e e2e-tests/test_health.py::test_orchestrator_health

# Keep resources after tests (useful for debugging)
uv run pytest -m e2e --no-cleanup

# Clean up namespace before deployment
uv run pytest -m e2e --clean-namespace

# Delete the namespace after tests
uv run pytest -m e2e --delete-namespace
```

### Verbose output

```bash
uv run pytest -m e2e -vv -s -o log_cli=true --log-cli-level=INFO
```

---

## CLI Parameters Reference

| Flag | Description |
|------|-------------|
| `--skip-build` | Skip building orchestrator and base server images |
| `--reuse-resources` | Skip `kubectl apply -k` — reuse current cluster resources |
| `--no-cleanup` | Skip teardown after tests (keeps resources for inspection) |
| `--clean-namespace` | Delete and recreate `idegym-local` namespace before setup |
| `--delete-namespace` | Delete `idegym-local` namespace in pytest teardown |
| `--delete-kustomize-services` | Delete only kustomize-managed services in pytest teardown |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `IDEGYM_TEST_BASE_URL` | `http://idegym-local.test` | Orchestrator URL |
| `IDEGYM_TEST_USERNAME` | `test` | Auth username |
| `IDEGYM_TEST_PASSWORD` | `test` | Auth password |
| `DOCKER_REGISTRY` | `registry.kube-system.svc.cluster.local` | Kaniko push registry |
| `KANIKO_INSECURE_REGISTRY` | `true` | Enable HTTP registry for Kaniko |

The kustomize overlay (`e2e-tests/config/`) sets `DOCKER_REGISTRY` and `KANIKO_INSECURE_REGISTRY`
automatically — no manual configuration needed.

---

## How It Works

### Image building

All images are built locally and loaded into Minikube — no external registry or credentials required.

**Orchestrator image:**
- Built from local source using `scripts/build_orchestrator_image.py`
- Tagged as `ghcr.io/jetbrains-research/idegym/orchestrator:latest`
- Loaded into Minikube with `minikube image load`

**Base server image:**
- Built from `Dockerfile.jinja` (Debian bookworm)
- Tagged as `ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest`
- Loaded into Minikube for pod execution
- Also pushed to the cluster-internal registry so Kaniko can use it as a base

**Test images (Kaniko path):**
- Built inside the cluster by Kaniko jobs
- Pushed to the cluster-internal registry (`registry.kube-system.svc.cluster.local`)
- Pulled into Minikube's containerd before pods start

**Test images (local Docker path):**
- Built on the developer machine using `IdeGYMDockerAPI.build_image()`
- Loaded directly into Minikube with `minikube image load`
- No registry involved

> **Important:** The `default` Docker builder must be active (not a containerized buildx builder)
> so that local images are accessible during builds. The test setup handles this automatically.

### Kubernetes deployment

The `e2e-tests/config/` kustomize overlay customizes the base manifests for local testing:

- **Namespace:** `idegym-local`
- **Image pull policy:** `IfNotPresent` (uses locally loaded images)
- **Ingress host:** `idegym-local.test`

Deployed resources:
- PostgreSQL (database)
- Orchestrator (API server)
- Prometheus (metrics)
- Grafana (dashboards)
- Tempo (distributed traces)

### Network access

```
test code → http://idegym-local.test
             ↓
          /etc/hosts: idegym-local.test → 127.0.0.1
             ↓
          minikube tunnel: LoadBalancer external IP = 127.0.0.1
             ↓
          ingress-nginx → orchestrator service
```

---

## Privileged Containers

The test infrastructure uses privileged Kubernetes jobs with `hostPath` mounts to interact with
Minikube's containerd runtime. This is needed for:

1. **Pushing the base server image to the cluster registry** (`registry-push-job` in `utils/build_images.py`):
   - Mounts `/run/containerd` and `/usr/bin/ctr` from the Minikube node
   - Uses `ctr` to export the image and `skopeo` to push it to the registry

2. **Pulling Kaniko-built images into containerd** (`registry-pull-job` in `conftest.py`):
   - Pulls the built image from the registry into the `k8s.io` containerd namespace

> **Note:** Privileged containers with `hostPath` mounts may be blocked in production clusters
> that enforce Pod Security Standards or OPA/Gatekeeper policies. These are only used in local
> Minikube development. For alternatives in restricted environments, use a real container registry
> accessible from both the host and cluster.

---

## Test Files

| File | Description |
|------|-------------|
| `test_health.py` | Orchestrator health check |
| `test_kaniko_build.py` | Kaniko image build and push |
| `test_python_api_build.py` | Python fluent API build + deploy (Kaniko and local Docker) |
| `test_server_lifecycle.py` | Server start, stop, restart |
| `test_server_strategies.py` | Server scheduling strategies |
| `test_openenv_websocket.py` | WebSocket protocol tests |

---

## Module Architecture

### `utils/constants.py`

Centralized configuration used across the test suite:

```python
DEFAULT_NAMESPACE = "idegym-local"
INGRESS_NAMESPACE = "ingress-nginx"
BASE_URL = "http://idegym-local.test"
DEFAULT_REQUEST_TIMEOUT = 60        # seconds
DEFAULT_SERVER_START_TIMEOUT = 600  # seconds
```

### `utils/k8s_client.py`

Synchronous wrappers around `kubernetes-asyncio` for namespace, pod, deployment, and service operations.
Handles both `app` and `app.kubernetes.io/name` label selectors.

### `utils/k8s_setup.py`

High-level setup/teardown:
- `setup_kubernetes_environment()` — complete cluster setup
- `cleanup_kubernetes_environment()` — resource cleanup
- `wait_for_service()` — wait for orchestrator to respond
- `wait_for_pod_ready()` / `wait_for_pod_deleted()` — pod lifecycle helpers

### `utils/idegym_utils.py`

Test utilities:
- `create_http_client(name, ...)` — create a configured IdeGYM client for a test
- `generate_test_id()` — generate a unique ID for test isolation

### `utils/build_images.py`

Image building and loading:
- `build_orchestrator_image()` — build and load orchestrator
- `build_base_server_image()` — build and load base server image
- `switch_to_default_docker_builder()` — ensure local Docker builder is active

---

## Troubleshooting

### Service not responding

1. Check the tunnel is running:
   ```bash
   ps aux | grep "minikube tunnel"
   ```
2. Verify the ingress controller has an external IP:
   ```bash
   kubectl get svc -n ingress-nginx ingress-nginx-controller
   # EXTERNAL-IP should be 127.0.0.1
   ```
3. Test connectivity:
   ```bash
   curl http://idegym-local.test/health
   ```

### Image build fails with authorization error

Docker is trying to pull the base image from a registry instead of using the local copy.
Check the active builder:

```bash
docker buildx ls   # "default *" should be active
```

If not, switch manually:

```bash
docker context use default
docker buildx use default
```

### Pods stuck in `ImagePullBackOff`

The image is not available in Minikube's containerd. Check:

```bash
minikube image ls | grep idegym
```

Rebuild and reload with a full run:

```bash
uv run pytest -m e2e
```

### Tests fail with SSL errors

Use `http://` not `https://` for local development:

```bash
export IDEGYM_TEST_BASE_URL=http://idegym-local.test
```

### IDE shows `utils.k8s_setup` as unresolved

Mark `e2e-tests/` as a Source Root in your IDE, or always run tests from the project root using
`uv run pytest -m e2e`.

---

## Adding New Tests

1. Create a test file in `e2e-tests/` following the `test_*.py` naming convention
2. Use `create_http_client()` from `utils/idegym_utils.py`
3. Mark with `@pytest.mark.asyncio` for async tests
4. Add a docstring explaining what the test validates

```python
# e2e-tests/test_my_feature.py
import pytest
from utils.idegym_utils import create_http_client


@pytest.mark.asyncio
async def test_my_feature(test_id):
    """Verify that my feature works end-to-end."""
    async with create_http_client(name=f"my-feature-{test_id}") as client:
        # test code
        pass
```

## Modifying Infrastructure

| What | Where |
|------|-------|
| Kubernetes resource changes | `e2e-tests/config/kustomization.yaml` |
| Image building logic | `e2e-tests/utils/build_images.py` |
| Cluster setup/teardown | `e2e-tests/utils/k8s_setup.py` |
| Shared constants | `e2e-tests/utils/constants.py` |
| Kubernetes API helpers | `e2e-tests/utils/k8s_client.py` |
