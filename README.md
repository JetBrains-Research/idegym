# IdeGYM

_IdeGYM_ is a framework offering disposable environments and tools for inspecting and modifying them.
These tools benefit AI agents and machine learning tasks.
They are useful for project work and for reinforcement learning, evaluation, and other pipelines.
Think of it as codespaces for AI agents.

IdeGYM prioritizes **scalability** and **speed**.
It aims to quickly create environments, which include a checked-out repository and a functioning IntelliJ IDEA instance,
while handling thousands of parallel environments, given sufficient infrastructure.

## Setting Up the Project

### Prerequisites

- Ensure you have [`uv`](https://github.com/astral-sh/uv) installed on your system.
- Ensure Docker is installed and running on your machine.

### Set Up Python

This project uses Python version `3.12`.
You can install it manually using your system package manager.
However, it's more convenient to run the following:

```sh
uv python install
```

### Create Virtual Environment

```sh
uv venv --seed
```

### Install Project Dependencies

```sh
uv sync --all-packages --all-extras --all-groups
```

### Install Pre-Commit Hooks

```sh
pre-commit install
```

## Development

### Checking Code Style

You can check code style and auto-fix issues with [`ruff`](https://github.com/astral-sh/ruff):

```sh
uv run ruff format
```

### Running Tests

Execute the following command to run tests:

```sh
uv run pytest
```

### Running Pre-Commit Hooks

To test `pre-commit` hooks:

```sh
pre-commit run
```
