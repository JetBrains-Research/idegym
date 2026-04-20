# Getting Started

This guide covers everything needed to set up a local development environment for IdeGYM,
run the test suites, and make your first code change.

## Prerequisites

### Required for all development

| Tool | Version | Purpose | Install |
|------|---------|---------|---------|
| [uv](https://github.com/astral-sh/uv) | >= 0.10.0 | Python package manager | See below |
| [Git](https://git-scm.com) | any | Version control | System package manager |

**Install `uv`** (the only tool you need to install manually):

```sh
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or with Homebrew
brew install uv

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

> `uv` will manage Python 3.12 for you — no separate Python install required.

### Required for integration tests

| Tool | Version | Purpose | Install |
|------|---------|---------|---------|
| [Docker](https://docs.docker.com/get-docker/) | >= 24 | Build and run containers | [Docker Desktop](https://docs.docker.com/desktop/) or `brew install docker` |

Integration tests build real Docker images and push them to a local registry. They require:

1. A running Docker daemon.
2. A container registry. Start one with:
   ```sh
   docker run -d -p 5000:5000 --name registry registry:2
   ```
   > [!WARNING]
   > On macOS, **AirPlay Receiver** (Control Center) occupies port 5000 by default.
   > If the command above fails with *address already in use*, use port 5001 instead:
   > ```sh
   > docker run -d -p 5001:5000 --name registry registry:2
   > ```
   > To free port 5000 permanently, turn off AirPlay Receiver in
   > **System Settings → General → AirDrop & Handoff → AirPlay Receiver**.
   > See also: [Port 5000 already in use](local_deployment.md#port-5000-already-in-use-when-starting-a-local-docker-registry).
3. The `IDEGYM_TEST_REGISTRY` environment variable set to the registry address:
   ```sh
   export IDEGYM_TEST_REGISTRY=localhost:5000   # or localhost:5001 on macOS
   ```

In CI this registry is provided automatically as a Docker service container. Locally you need to start it manually before running the integration suite.

### Required for e2e tests and local deployment

| Tool | Version | Purpose | Install |
|------|---------|---------|---------|
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | >= 1.28 | Kubernetes CLI | `brew install kubernetes-cli` |
| [minikube](https://minikube.sigs.k8s.io/docs/start/) | >= 1.35 | Local Kubernetes cluster | `brew install minikube` |

> See [Local Deployment](local_deployment.md) and [E2E Tests](../e2e-tests/README.md) for full setup instructions.

## Installation

### 1. Clone the repository

```sh
git clone https://github.com/JetBrains-Research/idegym.git
cd idegym
```

### 2. Install Python

`uv` manages the Python version declared in `.python-version` (3.12):

```sh
uv python install
```

### 3. Create a virtual environment

```sh
uv venv --seed
```

`--seed` pre-installs `pip`, `setuptools`, and `wheel` into the virtual environment alongside `uv`'s
own tooling. Some build backends and older packages rely on these tools being present; without `--seed`
they may not be available inside the venv.

This creates `.venv/` in the project root. Activate it if needed by your IDE or shell:

```sh
# Activate (optional — uv run handles this automatically)
source .venv/bin/activate   # Linux / macOS
.venv\Scripts\activate      # Windows
```

### 4. Install all dependencies

```sh
uv sync --all-packages --all-extras --all-groups
```

This installs every package across all workspace members, including dev and test dependencies.

### 5. Install pre-commit hooks

```sh
pre-commit install
```

Hooks run automatically on `git commit` to enforce code style.

## Running Tests

### Unit tests

Fast, self-contained tests with no external dependencies:

```sh
uv run pytest -m unit
```

### Integration tests

Tests that build Docker images and push them to a local registry.

Prerequisites (see [Required for integration tests](#required-for-integration-tests) above):
- Docker daemon running
- Local registry started (port 5000, or 5001 on macOS if AirPlay Receiver is active)
- `IDEGYM_TEST_REGISTRY` set to the registry address

```sh
IDEGYM_TEST_REGISTRY=localhost:5000 uv run pytest -m integration
# macOS with AirPlay Receiver enabled:
IDEGYM_TEST_REGISTRY=localhost:5001 uv run pytest -m integration
```

### End-to-end tests

E2E tests require a running Minikube cluster with specific addons. See [E2E Tests](../e2e-tests/README.md) for the full setup, then run:

```sh
uv run pytest -m e2e
```

Speed up subsequent runs by reusing a running cluster:

```sh
uv run pytest -m e2e --skip-build --reuse-resources
```

### Verbose output

```sh
uv run pytest -vv -s -o log_cli=true --log-cli-level=INFO
```

## Code Style

IdeGYM uses [Ruff](https://github.com/astral-sh/ruff) for formatting and linting.

**Configuration** (`pyproject.toml`):
- Line length: 120 characters
- Indent: 4 spaces
- Quotes: double
- Line endings: LF

### Auto-format code

```sh
uv run ruff format
```

### Check for lint errors

```sh
uv run ruff check
```

### Fix auto-fixable lint errors

```sh
uv run ruff check --fix
```

Pre-commit hooks run both `ruff format` and `ruff check` on every commit.

### Run all pre-commit hooks manually

```sh
pre-commit run --all-files
```

## Type Conventions

- Use `Optional[X]` for optional types (not `X | None`)
- Use built-in generics for non-optional: `dict[str, Any]`, `list[str]` (not `Dict`, `List`)
- Target Python 3.12+ features where appropriate

## IDE Setup

### PyCharm / IntelliJ IDEA

1. Open the project root as the project directory.
2. Set the Python interpreter to `.venv/bin/python`.
3. Mark `e2e-tests/` as a **Source Root** (right-click → Mark Directory As → Sources Root).
4. Install the [Kubernetes plugin](https://plugins.jetbrains.com/plugin/10485-kubernetes) for working with manifests.

### VS Code

1. Install the [Python extension](https://marketplace.visualstudio.com/items?itemName=ms-python.python).
2. Select the interpreter from `.venv/bin/python`.
3. The workspace is already configured — open the root folder.

## Package Manager Notes

This project uses `uv` exclusively. **Never use `pip` directly** — it bypasses `uv`'s lockfile and workspace management.

| Instead of | Use |
|---|---|
| `pip install <pkg>` | `uv add <pkg>` |
| `pip install -r requirements.txt` | `uv sync` |
| `python -m pytest` | `uv run pytest` |
| `python script.py` | `uv run python script.py` |

## Next Steps

- [Local Deployment](local_deployment.md) — run the full IdeGYM stack locally on Minikube
- [Image Builder](image_builder.md) — build custom environment images with plugins
- [Full Flow Example](full_flow_example.md) — see a complete build-deploy-run walkthrough
