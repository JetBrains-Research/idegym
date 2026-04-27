# Django IDEGym SweMini RL Training

Multi-turn reinforcement learning for Django method generation using an agentic SWE-style workflow with IDEGym.

## Overview

This example implements a **SweMini-style agent loop** that:
1. Allocates a persistent IDEGym server and cuts the target method
2. Runs an agentic loop: model generates bash commands → commands execute on server → observations returned
3. Supports two parsing modes: **toolcall** and **text** (see [Parsing Modes](#parsing-modes) below)
4. Agent explicitly submits via `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
5. Tests run only after submission, then rewards are computed

## Architecture

```
initialize → agent_step (loop) → run_tests → finalize → END
              ↑___________|
               (if should_continue)
```

Each `agent_step`:
1. Calls the LLM with conversation history
2. Parses bash commands from response (toolcall or text mode)
3. Executes commands on IDEGym server
4. Renders observations back to conversation

## Dataset

**[`JetBrains-Research/django_method_gen`](https://huggingface.co/datasets/JetBrains-Research/django_method_gen)** is a code generation benchmark built from the Django codebase. Each example is a task to regenerate a single Python method that has been cut from its class. The dataset provides the surrounding class code, file imports, and docstrings as context. Reward is rule-based: the agent's submission is evaluated by running the original unit tests.

The dataset follows the VERL multi-turn format: each row contains a `prompt` (chat-style system + user messages), an `agent_name` field (`"idegym_django"`), and an `extra_info` blob carrying the raw task data passed to the IDEGym server — including the method body to recover, file context, and test metadata. There are 1,364 training examples and 100 test examples, spanning four difficulty levels.

## Installation

```bash
cd examples/verl
uv sync
source .venv/bin/activate
```

For testing training without IDEGym, set `use_mock_runner: true` in the config.

## Usage

### Environment variables

Set the following environment variables (could provide in `.env` file in the root of the example):

```
ORCHESTRATOR_URL # URL of the orchestrator
IMAGE_TAG # Image tag in the registry with the target repo
IDEGYM_AUTH_USERNAME # Credentials for IDEGym server
IDEGYM_AUTH_PASSWORD
```

For the Django training setup you can either use the pre-built image from the registry:

```
IMAGE_TAG=ghcr.io/jetbrains-research/idegym/django-for-verl:latest
```

or build it yourself from the definition in `image/`:

```bash
# Build locally (requires Docker)
cd examples/verl/image
uv run python build.py

# Build and push to your own registry
uv run python build.py --push
```

See [`image/django.yaml`](image/django.yaml) for the full image definition and
[`image/build.py`](image/build.py) for the build script.

### Running

To test IDEgym:

`pytest test/test_idegym_runner_integration.py::test_idegym_smoke -v -s`

Very first run can take long time: ~ 2 min. Later the server creation should be ~7 s.

```bash
./run_django_idegym.sh
```

With mock runner (no IDEGym needed):
```bash
./run_django_idegym.sh actor_rollout_ref.rollout.use_mock_runner=true
```

## Key Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `rollout.max_turns` | 10 | Max agent interaction steps |
| `rollout.agent_parsing_mode` | "toolcall" | "toolcall" or "text" |
| `rollout.use_mock_runner` | false | Use mock IDEGym runner |
| `rollout.max_num_tests` | 10 | Max tests per item |
| `rollout.max_test_output_symb` | 10000 | Max chars of output in observations |
| `rollout.keep_reasoning` | "none" | Filter `<think>` blocks: "none", "last", "all" |
| `rollout.enable_thinking` | false | Enable extended thinking in tokenizer |
| `rollout.trajectory_dir` | null | S3 path for trajectory saving |

## Parsing Modes

The `agent_parsing_mode` setting controls how the agent's bash commands are extracted from the LLM response:

- **`toolcall`** (default) — The model uses structured tool calls (OpenAI-style `function_call` objects). The agent declares a `bash` tool and the model returns commands as tool call arguments. This is the recommended mode for models with strong tool-calling support (e.g. GPT-4, Qwen).

- **`text`** — The model embeds commands in fenced code blocks using the ` ```mswea_bash_command ` delimiter. The agent parses these blocks with a regex. This mode works with any text-generating model and does not require tool-call capabilities. Model expected to return ponly one such command in a step.

Both modes execute the same underlying bash commands on the IDEGym server; only the LLM output format differs. Prompt templates are loaded from `prompts/prompts_swemini_toolcall.yaml` or `prompts/prompts_swemini_text.yaml` accordingly.
