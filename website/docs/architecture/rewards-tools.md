---
title: Rewards & tools
description: What an agent can do inside an environment, and how its actions are scored for evaluation.
---

# Rewards & tools

This page answers two questions an RL or agent researcher actually has: **what can the
agent do inside an environment, and how do we score it?** Tools are the *actions*;
rewards are the *signal*.

## Act, then score (click a node for source)

```mermaid
flowchart TB
    subgraph tools["🛠️ Tools"]
        direction TB
        bash("<b>bash</b>"):::tool
        cf("<b>create_file</b>"):::tool
        ef("<b>edit_file</b>"):::tool
        pf("<b>patch_file</b>"):::tool
        inspect("<b>IDE inspect</b>"):::tool
    end

    subgraph rewards["🎯 Rewards"]
        direction TB
        comp("<b>compilation_reward</b>"):::tool
        setup("<b>setup_reward</b>"):::tool
        test("<b>test_reward</b>"):::tool
    end

    agent(["<b>🤖 Agent / trainer</b>"]):::client --> tools --> env[["<b>📦 Sandbox state</b>"]]:::pod
    env --> rewards --> signal["<b>📈 Training signal</b>"]:::infra

    classDef tool fill:#e23b3b,stroke:#c02626,color:#fff;
    classDef client fill:#2563eb,stroke:#1d4ed8,color:#fff;
    classDef pod fill:#4f46e5,stroke:#4338ca,color:#fff;
    classDef infra fill:#475569,stroke:#334155,color:#fff;

    click bash "https://github.com/JetBrains-Research/idegym/blob/main/tools/src/idegym/tools/tool_service.py" "View the bash tool-service source on GitHub."
    click cf "https://github.com/JetBrains-Research/idegym/blob/main/tools/src/idegym/tools/file_manager.py" "View the file-manager source on GitHub."
    click ef "https://github.com/JetBrains-Research/idegym/blob/main/tools/src/idegym/tools/file_manager.py" "View the file-manager source on GitHub."
    click pf "https://github.com/JetBrains-Research/idegym/blob/main/tools/src/idegym/tools/file_manager.py" "View the file-manager source on GitHub."
    click inspect "https://github.com/JetBrains-Research/idegym/blob/main/plugins/pycharm/src/idegym/plugins/pycharm/server.py" "View the IDE-inspection plugin source on GitHub."
    click comp "https://github.com/JetBrains-Research/idegym/blob/main/rewards/src/idegym/rewards/compilation_checker.py" "View the compilation-reward checker source on GitHub."
    click setup "https://github.com/JetBrains-Research/idegym/blob/main/rewards/src/idegym/rewards/setup_checker.py" "View the setup-reward checker source on GitHub."
    click test "https://github.com/JetBrains-Research/idegym/blob/main/rewards/src/idegym/rewards/test_checker.py" "View the test-reward checker source on GitHub."
```

## Tools — the action space

Every server ships the **tools** plugin, available on all images:

| Tool | Endpoint | What it does |
|---|---|---|
| Bash | `POST /api/tools/bash` | Run a bash script; returns bounded stdout/stderr and the exit code |
| Create file | `POST /api/tools/file/create` | Create a file with given content |
| Edit file | `POST /api/tools/file/edit` | Replace a 1-indexed, inclusive line range |
| Patch file | `POST /api/tools/file/patch` | Apply a unified diff |

The three file tools are **also exposed as MCP tools** (`create_file`, `edit_file`,
`patch_file`) on the server's `/mcp` endpoint. With a JetBrains IDE plugin installed, an
`inspect` endpoint runs the full IntelliJ static-analysis pipeline, and **mcp-steroid**
exposes the IntelliJ Platform API (PSI, refactoring, debugger, VCS) as MCP tools. See the
[tools reference](/reference/tools).

The Bash tool retains 1 MiB by default for each of stdout and stderr, split between the
beginning and end of each stream. Callers can choose another positive byte limit or request
complete output with `max_output_bytes=None`. A short marker reports omitted bytes when a
stream is truncated; the marker itself is outside the per-stream limit. Output is still
drained until completion or the execution timeout, preventing subprocess pipe deadlocks.

```python
result = await server.execute_bash("python -m pytest -q")
await server.patch_file("/home/devuser/project/main.py", patch="--- ...")
```

## Rewards — the evaluation signal

Three reward kinds turn the environment's state into a number an RL loop can train on.
Each runs a script **inside the sandbox** and returns a structured result.

| Reward | Call | Result |
|---|---|---|
| Compilation | `server.compilation_reward(compilation_script=...)` | `.success` (bool) |
| Setup | `server.setup_reward(setup_check_script=...)` | `.success` (bool) |
| Test | `server.test_reward(test_script=...)` | `.passed`, `.failed`, `.output` |

```python
result = await server.test_reward(test_script="cd /project && python -m pytest -q")
score = result.passed / (result.passed + result.failed)
```

Because the [orchestrator](/architecture/orchestrator) **persists every forwarded
request and response**, rewards can also be recomputed offline from stored runs — making
evaluation reproducible.

## How an episode uses both

A typical RL episode: `start_server` with `reuse_strategy=RESET` → the agent acts via
**tools** → call a **reward** → release the server for reuse. The full sequence is on the
[data flow page](/overview/data-flow#the-rl--eval-inner-loop).

## View source

- Tool service & file ops → [`tools/src/idegym/tools/`](https://github.com/JetBrains-Research/idegym/tree/main/tools/src/idegym/tools)
- Reward checkers → [`rewards/src/idegym/rewards/`](https://github.com/JetBrains-Research/idegym/tree/main/rewards/src/idegym/rewards)
- Full reference → [Tools docs](/reference/tools) · [Client rewards](/reference/client)
