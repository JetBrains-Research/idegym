# E2E Tests on Minikube

Integration tests for IdeGYM that run on a local minikube cluster without requiring remote registry access.

## Directory Map

```
e2e-tests-minikube/
├── README.md
├── __init__.py
├── pyproject.toml
├── run_tests.py
├── uv.lock
├── config/
│   └── kustomization.yaml
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_annotated_types.py
│   ├── test_health.py
│   ├── test_server_lifecycle.py
│   └── test_server_strategies.py
├── utils/
│   ├── __init__.py
│   ├── build_images.py
│   ├── idegym_utils.py
│   └── k8s_setup.py
```

## Prerequisites

### 1. Install Required Tools

Install Docker, kubectl, minikube, and uv:

```bash
brew install --cask docker
brew install kubernetes-cli minikube
brew install uv
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

### 5. Create Namespace

Create the dedicated namespace for e2e testing:

```bash
kubectl create namespace idegym-local
```

## Quick Start

Run all tests:

```bash
cd e2e-tests-minikube
uv run python run_tests.py
```

This will:
1. Build orchestrator and base server images
2. Load images into minikube
3. Deploy all Kubernetes resources
4. Wait for services to become ready
5. Run integration tests
6. Clean up resources

## Usage

### Basic Commands

```bash
# Run all tests
python run_tests.py

# Run specific tests (pytest -k expression)
python run_tests.py --test health
python run_tests.py --test "reuse and not limits"

# Skip image building (use existing images)
python run_tests.py --skip-build

# Reuse existing Kubernetes resources
python run_tests.py --reuse-resources

# Recreate namespace before setup
python run_tests.py --clean-namespace

# Keep resources after tests (for debugging)
python run_tests.py --no-cleanup

# Delete full namespace after all tests
python run_tests.py --delete-namespace

# Delete only kustomize services after all tests
python run_tests.py --delete-kustomize-services
```

### Cleanup Behavior

The post-test cleanup mode is selected by flags:

- Default (no cleanup flags): delete kustomization resources (`kubectl delete -k ...`)
- `--no-cleanup`: skip post-test cleanup
- `--delete-namespace`: pytest session teardown deletes `idegym-local` namespace
- `--delete-kustomize-services`: pytest session teardown deletes only services rendered by kustomize

Important:
- `--clean-namespace` is a pre-test setup option only (it resets namespace before deployment)
- If `--delete-namespace` or `--delete-kustomize-services` is set, the runner skips default cleanup to avoid duplicate deletion paths

### CLI Parameters

- `--skip-build`: Skip building orchestrator and base server images
- `--reuse-resources`: Skip `kubectl apply -k` and reuse current cluster resources
- `--test <expr>`: Pass expression to `pytest -k`
- `--no-cleanup`: Skip runner cleanup after tests
- `--clean-namespace`: Recreate `idegym-local` before deployment (setup phase)
- `--delete-namespace`: After tests, pytest teardown deletes `idegym-local`
- `--delete-kustomize-services`: After tests, pytest teardown deletes services from rendered kustomization

### Development Workflow

When iterating on tests:

```bash
# First run - builds everything
uv run python run_tests.py

# Subsequent runs - reuse infrastructure
uv run python run_tests.py --skip-build --reuse-resources

# After code changes - rebuild and test
uv run python run_tests.py --reuse-resources
```

Direct pytest (without orchestration script):

```bash
cd e2e-tests-minikube
uv run pytest tests -v -s -o addopts=
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
uv run python run_tests.py --no-cleanup
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
- Run tests via `uv run python run_tests.py` or `cd e2e-tests-minikube && uv run pytest ...`

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
- **Documentation:** Update this README
