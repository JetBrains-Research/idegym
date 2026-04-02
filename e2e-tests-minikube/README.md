# E2E Tests on Minikube

Integration tests for IdeGYM that run on a local minikube cluster without requiring remote registry access.

## Prerequisites

### 1. Install Required Tools

Install Docker, kubectl, minikube, and uv.
For Docker, install either Docker Desktop or the Docker formula:

```bash
brew install --cask docker-desktop
# or
brew install docker
brew install kubernetes-cli minikube uv
```

### 2. Start Minikube Cluster

Start the cluster with required addons:

```bash
minikube start \
  --addons=gvisor,ingress \
  --container-runtime=containerd \
  --docker-opt containerd=/var/run/containerd/containerd.sock \
  --kubernetes-version=v1.35.0
```

This will:
- Create a new Kubernetes cluster
- Install the gvisor and ingress addons
- Set up the containerd container runtime

### 3. Configure Host Access

Add the orchestrator hostname to `/etc/hosts`:

```bash
echo "127.0.0.1 idegym-local.test" | sudo tee -a /etc/hosts
```

> **Note:** This only needs to be done once. The entry persists across cluster restarts.

### 4. Start Minikube Tunnel

In a **separate terminal window**, start the tunnel (required for LoadBalancer services):

```bash
sudo minikube tunnel
```

> **Important:** Keep this terminal open while running tests. The tunnel must stay active.

### 5. Namespace Management

Pytest session setup creates the `idegym-local` namespace automatically when needed.

## Quick Start

Run all tests:

```bash
uv run pytest -m e2e
```

This will:
1. Build orchestrator and base server images
2. Load images into minikube
3. Deploy all Kubernetes resources
4. Wait for services to become ready
5. Run e2e tests
6. Clean up resources

## Usage

### Basic Commands

```bash
# Run all tests
uv run pytest -m e2e

# Run specific tests (pytest -k expression)
uv run pytest -m e2e -k health
uv run pytest -m e2e -k "reuse and not limits"

# Skip image building (use existing images)
uv run pytest -m e2e --skip-build

# Reuse existing Kubernetes resources
uv run pytest -m e2e --reuse-resources

# Recreate namespace before setup
uv run pytest -m e2e --clean-namespace

# Keep resources after tests (for debugging)
uv run pytest -m e2e --no-cleanup

# Delete full namespace after all tests
uv run pytest -m e2e --delete-namespace

# Delete only kustomize services after all tests
uv run pytest -m e2e --delete-kustomize-services
```

### Running Specific Tests

Run by test name/expression (`-k`):

```bash
uv run pytest -m e2e -k health
uv run pytest -m e2e -k "reuse and not limits"
```

Run a single test by node ID:

```bash
uv run pytest -m e2e e2e-tests-minikube/tests/test_health.py::test_orchestrator_health
```

### Showing All Logs

Use:

```bash
uv run pytest -m e2e -vv -s -o log_cli=true --log-cli-level=INFO
```

What each flag does:
- `-vv`: very verbose pytest output (shows test IDs and more details)
- `-s`: disable output capture so stdout/stderr is printed live
- `-o log_cli=true`: enable live logging to console
- `--log-cli-level=INFO`: show logs at INFO level and above

### Cleanup Behavior

The post-test cleanup mode is selected by flags:

- Default (no cleanup flags): delete kustomization resources (`kubectl delete -k ...`)
- `--no-cleanup`: skip post-test cleanup
- `--delete-namespace`: pytest session teardown deletes `idegym-local` namespace
- `--delete-kustomize-services`: pytest session teardown deletes only services rendered by kustomize

Important:
- `--clean-namespace` is a pre-test setup option only (it resets namespace before deployment)
- If `--delete-namespace` or `--delete-kustomize-services` is set, pytest teardown skips default cleanup to avoid duplicate deletion paths

### CLI Parameters

- `--skip-build`: Skip building orchestrator and base server images
- `--reuse-resources`: Skip `kubectl apply -k` and reuse current cluster resources
- `-k <expr>`: Run only tests matching a keyword expression
- `--no-cleanup`: Skip pytest teardown cleanup after tests
- `--clean-namespace`: Recreate `idegym-local` before deployment (setup phase)
- `--delete-namespace`: After tests, pytest teardown deletes `idegym-local`
- `--delete-kustomize-services`: After tests, pytest teardown deletes services from rendered kustomization

### Development Workflow

When iterating on tests:

```bash
# First run - builds everything
uv run pytest -m e2e

# Subsequent runs - reuse infrastructure
uv run pytest -m e2e --skip-build --reuse-resources

# After code changes - rebuild and test
uv run pytest -m e2e --reuse-resources
```

## How It Works

### Image Building

All images are built locally and loaded into minikube - no remote registry required:

**Orchestrator Image:**
- Built from local source code using top-level `scripts/build_orchestrator_image.py`
- Tagged as `ghcr.io/jetbrains-research/idegym/orchestrator:latest`
- Loaded into minikube with `minikube image load`

**Base Server Image:**
- Built from `Dockerfile.jinja` (debian:bookworm base)
- Tagged as `ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest`
- Available in local Docker for `IdeGYMDockerAPI` to use as base
- Loaded into minikube for pod execution

**Test Images:**
- Built during tests using `IdeGYMDockerAPI.build()`
- Uses the local base server image (no registry pull)
- Automatically loaded into minikube

> **Key:** The `default` docker builder has access to local images. Containerized buildx builders don't, so the test runner switches to `default` before running tests.

### Kubernetes Deployment

Uses kustomize to deploy resources with local customizations:
- **Namespace:** `idegym-local` (isolated from other deployments)
- **Image pull policy:** `IfNotPresent` (uses local images)
- **Ingress host:** `idegym-local.test`
- **Ingress controller:** LoadBalancer type (works with minikube tunnel)

Deployed resources:
- PostgreSQL database
- Orchestrator API
- Prometheus (metrics)
- Grafana (visualization)
- Tempo (tracing)

### Kubernetes API Usage

- `kubernetes-asyncio` is used for namespace, pod, deployment, service, PDB, and ReplicaSet operations in test setup/teardown code.
- `kubectl` is still used for kustomize workflows (`kubectl apply -k`, `kubectl delete -k`, `kubectl kustomize`) because the Python client does not provide native kustomize rendering/apply behavior.

### Network Access

Tests connect to `http://idegym-local.test`:
1. `/etc/hosts` maps `idegym-local.test` → `127.0.0.1`
2. `minikube tunnel` assigns `127.0.0.1` as external IP for LoadBalancer
3. Ingress controller routes requests to orchestrator service

## Environment Variables

Configure test behavior with environment variables:

```bash
# Orchestrator URL (default: http://idegym-local.test)
export IDEGYM_TEST_BASE_URL=http://idegym-local.test

# Authentication credentials (default: test/test)
export IDEGYM_TEST_USERNAME=test
export IDEGYM_TEST_PASSWORD=test
```

## Troubleshooting

### Service not responding

1. Check minikube tunnel is running:
   ```bash
   ps aux | grep "minikube tunnel"
   ```

2. Verify external IP is assigned:
   ```bash
   kubectl get svc -n ingress-nginx ingress-nginx-controller
   ```
   Should show `EXTERNAL-IP: 127.0.0.1`

3. Test connection:
   ```bash
   curl http://idegym-local.test/health
   ```
   Should return `{"status":"healthy"}`

### Image build fails with authorization error

Docker is trying to pull the base image from registry instead of using local.

Verify the docker builder:
```bash
docker buildx ls
# Should show "default" with a "*" next to it
```

If not, manually switch:
```bash
docker context use default
docker buildx use default
```

### Pods stuck in ImagePullBackOff

Images weren't loaded into minikube. Check:
```bash
minikube image ls | grep idegym
```

Rebuild with:
```bash
uv run pytest -m e2e --no-cleanup
```

### Tests fail with SSL errors

Ensure the base URL uses `http://` not `https://`:
```bash
export IDEGYM_TEST_BASE_URL=http://idegym-local.test
```

### IDE shows `utils.k8s_setup` as unresolved

If PyCharm highlights `from utils.k8s_setup import ...` in `tests/conftest.py`:

- Mark `e2e-tests-minikube` as a Source Root in the IDE
- Use a pytest run configuration with working directory set to `e2e-tests-minikube`
- Run tests via `uv run pytest -m e2e` from the project root

## Contributing

### Adding New Tests

1. Create test file in `tests/` directory
2. Use `create_http_client()` from `utils/idegym_utils.py`
3. Follow naming convention: `test_*.py`
4. Add docstring explaining what the test validates

Example:

```python
# tests/test_my_feature.py
from utils.idegym_utils import create_http_client
import pytest


@pytest.mark.asyncio
async def test_my_feature():
    """Test that my feature works correctly."""
    async with create_http_client(name="test-client") as client:
        # Your test code
        result = await client.some_operation()
        assert result.success
```

### Modifying Infrastructure

- **Kubernetes changes:** Update `config/kustomization.yaml`
- **Image building:** Update `utils/build_images.py`
- **Deployment logic:** Update `utils/k8s_setup.py`
- **Shared constants:** Update `utils/constants.py` (namespaces, timeouts, labels, URLs)
- **Kubernetes API helpers:** Update `utils/k8s_client.py`
- **Documentation:** Update this README

## Module Architecture

### utils/constants.py

Centralized configuration values used across the test suite:

```python
# Kubernetes configuration
DEFAULT_NAMESPACE = "idegym-local"
INGRESS_NAMESPACE = "ingress-nginx"

# URLs
BASE_URL = "http://idegym-local.test"

# Timeouts (in seconds)
DEFAULT_REQUEST_TIMEOUT = 60
DEFAULT_SERVER_START_TIMEOUT = 600

# Pod labels
ORCHESTRATOR_APP_LABEL = "orchestrator"
SERVER_CONTAINER_NAME = "server"
```

### utils/k8s_client.py

Synchronous wrappers around `kubernetes-asyncio` for:
- Namespace operations (create, delete, check existence)
- Pod operations (list, delete, wait for deletion)
- Deployment and service cleanup
- Pod selector resolution (handles both `app` and `app.kubernetes.io/name` labels)

### utils/k8s_setup.py

High-level setup and teardown functions:
- `setup_kubernetes_environment()` - Complete environment setup
- `wait_for_service()` - Wait for orchestrator to become responsive
- `wait_for_pod_ready()` / `wait_for_pod_deleted()` - Pod lifecycle helpers
- `cleanup_kubernetes_environment()` - Resource cleanup

### utils/idegym_utils.py

Test utilities:
- `create_http_client()` - Create configured IdeGYM client for tests
- `generate_test_id()` - Generate unique IDs for test isolation

### utils/build_images.py

Image building:
- `build_orchestrator_image()` - Build and load orchestrator image
- `build_base_server_image()` - Build and load base server image
- `switch_to_default_docker_builder()` - Ensure local image access
