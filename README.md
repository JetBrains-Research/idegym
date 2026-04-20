# IdeGYM

_IdeGYM_ is a framework for creating **disposable, scalable development environments** for training
reinforcement learning models. It provides tools for inspecting and modifying those environments,
and can also be used for running AI agents or any workflow that requires clean, reproducible workspaces.

Think of it as **GitHub Codespaces for RL training** — but designed for thousands of parallel, short-lived environments.

## Key Features

- **Scalable orchestration** — spin up and tear down Kubernetes-based environments on demand
- **Plugin-based image builder** — compose Docker images from reusable plugins via a Python API or YAML
- **Flexible project loading** — clone from Git, download and extract a project archive, or mount a volume with a project directly into the image
- **HTTP and WebSocket forwarding** — the orchestrator proxies requests directly to running server pods; WebSocket support enables integration with [OpenEnv](https://github.com/openenv)-compatible environments
- **Persistent request history** — every forwarded request and its response is stored in the database and retrievable later, enabling offline reward computation and reproducible evaluation
- **Automatic resource cleanup** — a background watcher periodically reconciles the database against live Kubernetes state, evicting stale servers and reclaiming resources without manual intervention
- **Full observability** — built-in Prometheus metrics, Grafana dashboards, and distributed tracing via Tempo
- **Fast iteration** — local development with Minikube mirrors the production Kubernetes setup

## Documentation

| Guide | Description |
|---|---|
| [Getting Started](documentation/getting_started.md) | Prerequisites, installation, and running tests locally |
| [Local Deployment](documentation/local_deployment.md) | Run the full stack on Minikube (with GHCR images or local builds) |
| [Remote Deployment](documentation/remote_deployment.md) | Deploy to a production Kubernetes cluster |
| [Image Builder](documentation/image_builder.md) | Build custom environment images with the plugin API |
| [Client Library](documentation/client.md) | Python client API reference |
| [Full Flow Example](documentation/full_flow_example.md) | End-to-end walkthrough: build an image, start a server, run a command |
| [E2E Tests](e2e-tests/README.md) | Running the end-to-end test suite on Minikube |
| [Orchestrator API](orchestrator/README.md) | REST API reference for the orchestrator service |
| [HTTP Error Codes](documentation/http_error_codes.md) | HTTP status codes for orchestrator and server endpoints |

## Quick Start

### Prerequisites

- [`uv`](https://github.com/astral-sh/uv) >= 0.10.0 — Python package and project manager
- Python 3.12 (installed automatically by `uv`)
- [Docker](https://docs.docker.com/get-docker/) — for integration tests and local image builds

### Install

```sh
# Clone the repository
git clone https://github.com/JetBrains-Research/idegym.git
cd idegym

# Install Python 3.12 and project dependencies
uv python install
uv venv --seed
uv sync --all-packages --all-extras --all-groups

# Install pre-commit hooks
uv run pre-commit install
```

### Run Tests

```sh
# Unit tests only (no external dependencies)
uv run pytest -m unit

# Integration tests (requires Docker with a registry on localhost:5000)
uv run pytest -m integration

# End-to-end tests (requires a running Minikube cluster)
uv run pytest -m e2e
```

See [Getting Started](documentation/getting_started.md) for per-suite prerequisites.

### Check Code Style

```sh
uv run ruff format
uv run ruff check
```

## Project Structure

```
idegym/
├── api/                  # Pydantic API models
├── backend-utils/        # Shared backend utilities (Kubernetes, telemetry)
├── client/               # Python client library
├── common-utils/         # Shared utilities (config, logging)
├── examples/             # Runnable integration examples (standalone, not part of the workspace)
├── image-builder/        # Plugin-based Docker image building system
├── orchestrator/         # Kubernetes orchestrator service (FastAPI + PostgreSQL)
├── rewards/              # Reward calculation for agent evaluation
├── server/               # IdeGYM server (runs inside containers)
├── tools/                # Tool implementations (bash, file operations)
├── unit-tests/           # Unit test suite
├── integration-tests/    # Docker-based integration tests
├── e2e-tests/            # Kubernetes end-to-end tests
├── scripts/              # Build and deployment scripts
└── documentation/        # Extended documentation
```

## Examples

The [`examples/`](examples/README.md) directory contains standalone, runnable examples showing
how to integrate external environments with IdeGYM. It is intentionally kept separate from the
main uv workspace because the OpenEnv environment packages have transitive dependencies that
conflict with the backend infrastructure packages.

See [examples/README.md](examples/README.md) for available integrations and setup instructions.

## Contributing

We welcome contributions! Please open an issue or pull request on
[GitHub](https://github.com/JetBrains-Research/idegym).

Before submitting a pull request:
1. Run `uv run ruff format && uv run ruff check` to fix style issues
2. Run `uv run pytest -m "unit or integration"` to verify tests pass
3. Ensure pre-commit hooks pass: `pre-commit run --all-files`

## License

See [LICENSE](LICENSE) for details.
