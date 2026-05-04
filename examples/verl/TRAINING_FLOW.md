# Django IDEGym Training: How It Works

This document explains the training pipeline launched by `run_django_idegym.sh`.

---

## Overview

The training uses **GRPO** (Group Relative Policy Optimization) to teach a language model to implement Python methods in a Django codebase by interacting with a real execution environment. The model receives a coding task, executes bash commands in an isolated cloud server, and gets rewarded based on how many tests pass.

---

## Entry Point: `run_django_idegym.sh`

```bash
python3 -m verl.trainer.main_ppo \
    --config-path .../django_idegym_grpo.yaml \
    data.train_files=... \
    actor_rollout_ref.model.path=Qwen/Qwen3-0.6B \
    trainer.n_gpus_per_node=4
```

Sets `CUDA_VISIBLE_DEVICES`, builds `PYTHONPATH`, then launches the verl PPO trainer with:
- **Model**: `Qwen/Qwen3-0.6B`
- **Algorithm**: GRPO
- **Dataset**: `JetBrains-Research/django_method_gen` (HuggingFace Hub)
- **Agent loop config**: `config/agent_loop_config.yaml`

---

## Configuration Files

### `django_idegym_grpo.yaml` — Main Training Config

Defines the full training setup:

| Setting | Value |
|---------|-------|
| Train dataset | `JetBrains-Research/django_method_gen:train` |
| Test dataset | `JetBrains-Research/django_method_gen:test` |
| Dataset class | `HFHubDataset` (from `hf_dataset.py`) |
| Max prompt tokens | 16 000 |
| Max response tokens | 4 096 |
| Multi-turn | Enabled, Hermes chat format, max 10 turns |
| Agent loop | `idegym_django` → `SWEMiniDjangoAgentLoop` |

### `config/agent_loop_config.yaml` — Agent Behavior

| Setting | Value | Meaning |
|---------|-------|---------|
| `enable_thinking` | `false` | No `<think>` blocks |
| `max_turns` | `10` | Max agent steps per task |
| `max_num_tests` | `20` | Tests to run per task |
| `max_test_output_symb` | `10000` | Max observation size (chars) |
| `agent_parsing_mode` | `toolcall` | Extract commands from tool calls |
| `keep_reasoning` | `none` | Strip `<think>` blocks |

---

## Files and Classes

### `hf_dataset.py` — `HFHubDataset`

Inherits from verl's `RLHFDataset`. Accepts dataset paths in `"owner/repo:split"` format and loads them directly from HuggingFace Hub (no local download). Filters out samples that exceed the max prompt length.

---

### `agent_loop/swemini_django_agent_loop.py` — `SWEMiniDjangoAgentLoop`

The **core training loop**. Inherits from `AgentLoopBase` and uses verl's `AsyncLLMServerManager` directly (no LangChain/LangGraph wrapper). Runs a simple `while` loop:

```
initialize → agent_step (while should_continue) → run_tests → finalize
```

#### State

A plain `dict` tracking the full episode:
- `messages` — conversation history as OpenAI-format dicts (`{"role": "...", "content": "..."}`)
- `prompt_ids` / `response_mask` — accumulated token trajectory for RL training
- `turn` — current step count
- `should_continue` — loop control flag
- `test_results` — output from test execution
- `trajectory` — log of all actions taken
- Server/client handles, timestamps, reward score

#### `_initialize()`

1. Creates an **IDEGym client** (connects to the cloud orchestrator).
2. Allocates an **IDEGym server** (isolated container with the Django codebase).
3. **Cuts the target method** — replaces the method body with an empty stub via `edit_file()`.
4. Renders the **system prompt** and **task prompt** from Jinja templates.
   - System message: tool-use instructions, rules, examples.
   - Instance message: target class/method, description, recommended workflow.
5. On failure: marks the rollout as failed and skips to finalize.

#### `_agent_step()` (loop)

Runs up to `max_turns` times:

1. **Call the model** via `_call_model()`:
   - First turn: `self.apply_chat_template(messages, tools=...)` encodes the full conversation to token IDs.
   - `self.server_manager.generate(request_id, prompt_ids, sampling_params)` generates response tokens.
   - verl's `ToolParser` decodes response tokens into content + function calls.
   - Follow-up turns: observation tokens are appended via `self.apply_chat_template(obs_msgs, remove_system_prompt=True)`.
   - `prompt_ids` and `response_mask` accumulate directly in state (1 for LLM tokens, 0 for tool response tokens).
2. **Parse actions** using the configured strategy (`ToolcallStrategy` or `TextStrategy`):
   - **ToolcallStrategy** (default): validates `FunctionCall` objects from `ToolParser`. Tool schema: `bash(command: str)`.
   - **TextStrategy**: regex-matches ` ```bash ... ``` ` blocks from decoded content.
   - Raises `FormatError` if the format is wrong (error is shown to the model as an observation).
3. **Execute commands** on the IDEGym server via `IDEGymRunner.run_bash()`.
   - If the command is `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`, the loop stops with `stop_reason = "submitted"`.
4. **Render the observation** (stdout/stderr/exit code, truncated to `max_test_output_symb`).
5. Append to message history, encode observation tokens, and increment `turn`.

#### `_run_tests()`

Only runs if the agent submitted (not crashed/timed out):

1. Executes `python tests/runtests.py <tests> --verbosity 0 --noinput` on the server.
2. Parses output with `parse_idegym_tests_output()` → extracts pass/fail counts.
3. Computes `percentage_passed`.

#### `_finalize()`

1. Closes the IDEGym server and client.
2. **Calculates reward**: reward = `percentage_passed` (0.0–1.0).
3. Returns `AgentLoopOutput` built directly from accumulated `prompt_ids` and `response_mask`.

---

### `agent_loop/agent_parsing_strategies.py`

Two pluggable parsing strategies, selected by `agent_parsing_mode` config:

| Strategy | How it parses | Observation format |
|----------|--------------|-------------------|
| `ToolcallStrategy` | `FunctionCall` objects from `ToolParser` | `{"role": "tool", ...}` dicts |
| `TextStrategy` | Regex on ` ```bash ``` ` blocks | `{"role": "user", ...}` dicts |

Both strip `<think>...</think>` blocks and raise `FormatError` on malformed output.

---

### `agent_loop/idegym_runner.py` — `IDEGymRunner`

Manages cloud execution containers via the IDEGym orchestrator.

**Configuration (env vars):**
- `ORCHESTRATOR_URL` — server endpoint (required)
- `NAMESPACE` — Kubernetes namespace (default: `idegym`)
- `IMAGE_TAG` — Docker image for the server container
- `REPO` — repository type (`django`, `sympy`, …)

**Key methods:**

| Method | What it does |
|--------|-------------|
| `create_client()` | Connects to orchestrator, health-checks |
| `create_server(client)` | Allocates a persistent container (RESET strategy) |
| `run_bash(server, command)` | Executes a bash command, returns `{stdout, stderr, exit_code}` |
| `run_tests(server, item)` | Runs Django tests with retry (3 attempts, 1 s delay) |
| `edit_file(server, item)` | Replaces `start_line:end_line` with `replace_content` |
| `finish_server(server)` | Stops and cleans up the container |

---

### `agent_loop/idegym_runner_utils.py` — `ItemToRun`

Dataclass describing a single code-generation task:

```python
@dataclass
class ItemToRun:
    idx: int            # sample index
    dp_id: str          # data point ID
    file_path: str      # file in Django repo
    replace_content: str  # code to insert
    method_name: str    # function name
    start_line: int     # 1-indexed start line
    end_line: int       # 1-indexed end line (inclusive)
    tests: list[str]    # test paths to run
```

---

### `utils/postprocessing.py`

Parsing utilities used in finalization:

- `parse_idegym_tests_output()` — regex parser for Django test runner output; extracts per-test status and summary counts.
- `get_percentage_passed()` — extracts pass rate from test summary line.

In the agent loop (`SWEMiniDjangoAgentLoop`), the reward is simply `percentage_passed`.

---

## Training Data Flow

```
run_django_idegym.sh
    └── verl.trainer.main_ppo
            │
            ├── HFHubDataset          loads JetBrains-Research/django_method_gen
            │
            └── per batch: SWEMiniDjangoAgentLoop.run()
                    │
                    ├── _initialize()
                    │       ├── IDEGymRunner.create_client()
                    │       ├── IDEGymRunner.create_server()
                    │       └── IDEGymRunner.edit_file()  ← cuts method body
                    │
                    ├── _agent_step()  ×(1..10 turns)
                    │       ├── server_manager.generate() → ToolParser
                    │       ├── ToolcallStrategy.parse_actions()
                    │       └── IDEGymRunner.run_bash()
                    │
                    ├── _run_tests()
                    │       └── IDEGymRunner.run_tests()
                    │               └── python tests/runtests.py ...
                    │
                    └── _finalize()
                            ├── reward = percentage_passed
                            └── AgentLoopOutput → GRPO loss
```

---

## Episode Lifecycle

1. **Task arrives**: a Django method that the model must implement.
2. **Environment setup**: IDEGym container gets the method body erased, leaving only the signature.
3. **Agent loop**: the model explores the codebase with bash commands (read files, write edits, run scripts) until it calls `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` or hits `max_turns`.
4. **Tests run**: Django's test runner evaluates the implementation.
5. **Reward returned**: fraction of tests passed → GRPO updates the model weights.
