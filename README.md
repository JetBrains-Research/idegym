# IdeGYM

_IdeGYM_ is a framework for creating **disposable, scalable development environments** for training
reinforcement learning models. It provides tools for inspecting and modifying those environments,
and can also be used for running AI agents or any workflow that requires clean, reproducible workspaces.

Think of it as **GitHub Codespaces for RL training** — but designed for thousands of parallel, short-lived environments.

> 📖 **Docs & interactive architecture → [jetbrains-research.github.io/idegym](https://jetbrains-research.github.io/idegym/)**
> The site is built from [`website/`](website/); the reference guides live under [`website/docs/reference/`](website/docs/reference/).

## Key Features

- **Scalable orchestration** — spin up and tear down Kubernetes-based environments on demand
- **Plugin-based image builder** — compose Docker images from reusable plugins via a Python API or YAML; see [Plugin Architecture](https://jetbrains-research.github.io/idegym/reference/plugins)
- **Flexible project loading** — clone from Git, download and extract a project archive, or mount a volume with a project directly into the image; see [Image Builder](https://jetbrains-research.github.io/idegym/reference/image_builder)
- **HTTP and WebSocket forwarding** — the orchestrator proxies requests directly to running server pods; WebSocket support enables integration with [OpenEnv](https://github.com/meta-pytorch/OpenEnv)-compatible environments
- **Persistent request history** — every forwarded request and its response is stored in the database and retrievable later, enabling offline reward computation and reproducible evaluation
- **Automatic resource cleanup** — a background watcher periodically reconciles the database against live Kubernetes state, evicting stale servers and reclaiming resources without manual intervention
- **Full observability** — built-in Prometheus metrics, Grafana dashboards, and distributed tracing via Tempo
- **MCP interface** — the orchestrator exposes an MCP server at `/mcp`; agents can discover and call all IdeGYM operations as MCP tools without touching the REST API directly; see [MCP Server](https://jetbrains-research.github.io/idegym/reference/mcp)
- **IDE integration** — optional IntelliJ IDEA and PyCharm plugins add live code inspection endpoints and typed client methods for IDE-aware environments; see [Tools Reference](https://jetbrains-research.github.io/idegym/reference/tools#ide-inspection-inspectsh)
- **Server checkpoint/restore** — snapshot a warmed-up server and restore future servers from it, skipping cold-start work like project indexing; see [Pod Snapshots](orchestrator/README.md#pod-snapshots-checkpointrestore)
- **Fast iteration** — local development with Minikube mirrors the production Kubernetes setup

## Documentation

Everything lives on the **[IdeGYM site](https://jetbrains-research.github.io/idegym/)** — an
interactive, drill-down architecture diagram plus the guides below. The site is built from
[`website/`](website/), and the reference guides are the relocated docs under
[`website/docs/reference/`](website/docs/reference/).

| Guide | Description |
|---|---|
| [Architecture](https://jetbrains-research.github.io/idegym/architecture) | Interactive system diagram — click a component to drill in |
| [Core Concepts](https://jetbrains-research.github.io/idegym/overview/concepts) | Plain-language glossary of every moving part |
| [Data & Usage Flow](https://jetbrains-research.github.io/idegym/overview/data-flow) | The end-to-end RL / eval lifecycle |
| [Getting Started](https://jetbrains-research.github.io/idegym/reference/getting_started) | Prerequisites, installation, and running tests locally |
| [Local Deployment](https://jetbrains-research.github.io/idegym/reference/local_deployment) | Run the full stack on Minikube (with GHCR images or local builds) |
| [Remote Deployment](https://jetbrains-research.github.io/idegym/reference/remote_deployment) | Deploy to a production Kubernetes cluster |
| [Image Builder](https://jetbrains-research.github.io/idegym/reference/image_builder) | Build custom environment images with the plugin API |
| [Client Library](https://jetbrains-research.github.io/idegym/reference/client) | Python client API reference |
| [MCP Server](https://jetbrains-research.github.io/idegym/reference/mcp) | Tool-based access to orchestrator operations |
| [Tools Reference](https://jetbrains-research.github.io/idegym/reference/tools) | All tools available on IdeGYM servers: bash, file ops, IDE inspection, mcp-steroid |
| [Full Flow Example](https://jetbrains-research.github.io/idegym/reference/full_flow_example) | End-to-end walkthrough: build an image, start a server, run a command |
| [Plugin Architecture](https://jetbrains-research.github.io/idegym/reference/plugins) | Extending IdeGYM with image, server, and client plugins |
| [HTTP Error Codes](https://jetbrains-research.github.io/idegym/reference/http_error_codes) | HTTP status codes for orchestrator and server endpoints |
| [API Reference](https://jetbrains-research.github.io/idegym/api) | Interactive OpenAPI for the orchestrator and in-pod server |
| [E2E Tests](e2e-tests/README.md) | Running the end-to-end test suite on Minikube |
| [Orchestrator API](orchestrator/README.md) | REST API reference for the orchestrator service |

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

See [Getting Started](https://jetbrains-research.github.io/idegym/reference/getting_started) for per-suite prerequisites.

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
├── plugins/              # IDE plugins (IntelliJ IDEA, PyCharm, shared utilities)
├── charts/               # Helm charts for Kubernetes deployment
├── scripts/              # Build and deployment scripts
└── website/              # Docs & presentation site (Docusaurus); guides in website/docs/reference/
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
