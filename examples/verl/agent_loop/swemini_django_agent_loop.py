"""
Django IDEGym SweMini agent loop for verl.

Implements a multi-turn agentic workflow using verl's LLM server directly:
  initialize → agent_step (loop) → run_tests → finalize

The model generates bash commands executed on a persistent IDEGym server.
After the agent submits (or max turns), tests are run and rewards computed.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
import traceback
import uuid
from datetime import datetime
from typing import Any

from jinja2 import Environment as JinjaEnvironment
from jinja2 import StrictUndefined

from examples.verl.agent_loop.agent_parsing_strategies import (
    FormatError,
    ParsingStrategy,
    TextStrategy,
    ToolcallStrategy,
)
from examples.verl.prompts.swemini_prompts import load_prompts
from examples.verl.agent_loop.idegym_runner_utils import ItemToRun
from examples.verl.utils.postprocessing import (
    apply_reasoning_filter,
    extract_bash_output,
    get_percentage_passed,
    parse_idegym_tests_output,
)
from verl.experimental.agent_loop.agent_loop import AgentLoopBase, AgentLoopOutput, register
from verl.experimental.agent_loop.tool_parser import ToolParser

logger = logging.getLogger(__name__)

SUBMIT_SIGNAL = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
FAILED_PIPELINE_SCORE = 0

KEEP_REASONING_OPTIONS = {"none", "all"} # "last",

_CONTAINER_SYSTEM_INFO = {
    "system": "Linux",
    "release": "5.15.0-69-generic",
    "version": "#76-Ubuntu SMP Fri Mar 17 17:19:29 UTC 2023",
    "machine": "x86_64",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def count_decorators_above(file_lines: list[str], def_line_idx: int) -> int:
    """Count @decorator lines immediately above a function definition."""
    count = 0
    idx = def_line_idx - 1
    while idx >= 0:
        stripped = file_lines[idx].strip()
        if stripped.startswith("@"):
            count += 1
            idx -= 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# SWEMiniDjangoAgentLoop — registered for verl
# ---------------------------------------------------------------------------


@register("django_agent_loop")
class SWEMiniDjangoAgentLoop(AgentLoopBase):
    """SweMini-style multi-turn Django agent loop with IDEGym server interaction.

    Uses verl's AsyncLLMServerManager directly instead of a LangChain ChatModel wrapper.
    Tracks prompt_ids and response_mask explicitly for RL training.
    """

    def __init__(self, trainer_config, server_manager, tokenizer, processor, dataset_cls, data_config, **kwargs):
        super().__init__(trainer_config, server_manager, tokenizer, processor, dataset_cls, data_config, **kwargs)

        rollout_cfg = self.rollout_config

        self.max_turns = kwargs.get("max_turns", 10)
        self.max_num_tests = kwargs.get("max_num_tests", 10)
        self.max_test_output_symb = kwargs.get("max_test_output_symb", 10_000)
        self.enable_thinking = kwargs.get("enable_thinking", False)

        keep_reasoning = kwargs.get("keep_reasoning", "none")
        if keep_reasoning not in KEEP_REASONING_OPTIONS:
            keep_reasoning = "none"
        self.keep_reasoning = keep_reasoning

        # Parsing mode
        agent_parsing_mode = kwargs.get("agent_parsing_mode", "toolcall")
        self.agent_parsing_mode = agent_parsing_mode

        # Prompts
        prompts_file = kwargs.get("prompts_file", None)
        self.agent_prompts = load_prompts(agent_parsing_mode, prompts_file)

        self._jinja_env = JinjaEnvironment(undefined=StrictUndefined)
        self._jinja_env.filters["tojson"] = lambda v, *a, **kw: json.dumps(v, ensure_ascii=False)

        format_error_tpl = self.agent_prompts["format_error_template"]
        if agent_parsing_mode == "toolcall":
            self._strategy: ParsingStrategy = ToolcallStrategy(format_error_tpl)
        elif agent_parsing_mode == "text":
            self._strategy = TextStrategy(format_error_tpl)
        else:
            raise KeyError(f"Unknown agent_parsing_mode: {agent_parsing_mode}. Use: toolcall or text")

        # Tool parser for decoding LLM response tokens
        self._tool_parser = ToolParser.get_tool_parser(rollout_cfg.multi_turn.format, self.tokenizer)

        # Override apply_chat_template_kwargs to pass enable_thinking.
        # Always propagate it so models like Qwen3 can suppress thinking blocks when False.
        self.apply_chat_template_kwargs = {**self.apply_chat_template_kwargs, "enable_thinking": self.enable_thinking}

        # Local trajectory saving
        self.traj_local_path = None
        trajectory_dir = kwargs.get("trajectory_dir", None)
        if trajectory_dir:
            os.makedirs(trajectory_dir, exist_ok=True)
            # timestamp = datetime.now().strftime("%Y%m%d_%H%M")
            experiment_name = getattr(getattr(self.config, "trainer", None), "experiment_name", "default")
            filename = f"traj_django_swemini_{experiment_name}.jsonl"
            self.traj_local_path = os.path.join(trajectory_dir, filename)
            logger.info(f"[SCAFFOLD] Trajectories will be saved to {self.traj_local_path}")

        # IDEGym runner
        use_mock_runner = kwargs.get("use_mock_runner", False)
        if use_mock_runner:
            from examples.verl.agent_loop.mock_idegym_runner import MockIDEGymRunner
            mock_error_rate = kwargs.get("mock_runner_error_rate", 0.001)
            mock_pass_rate = kwargs.get("mock_runner_pass_rate", 0.7)
            logger.info(f"[SCAFFOLD] Using MockIDEGymRunner (error_rate={mock_error_rate}, pass_rate={mock_pass_rate})")
            self.idegym_runner = MockIDEGymRunner(error_rate=mock_error_rate, pass_rate=mock_pass_rate)
        else:
            from examples.verl.agent_loop.idegym_runner import IDEGymRunner
            self.idegym_runner = IDEGymRunner()

    # --- Prompt rendering ---

    def _jinja_render(self, template_str: str, **kwargs) -> str:
        return self._jinja_env.from_string(template_str).render(**kwargs)

    def _get_hint(self, item: dict) -> str:
        hint_level = item.get("hint_level", 0)
        if hint_level == 0:
            return ""
        method_body = item["method"]["body"]
        method_declaration = item["method"]["declaration"]
        if hint_level == 4:
            return method_declaration + "\n" + method_body
        lines = method_body.splitlines()
        total_lines = len(lines)
        ratio = hint_level / 4.0
        num_lines = max(1, round(total_lines * ratio))
        return method_declaration + "\n" + "\n".join(lines[:num_lines])

    def _render_instance_prompt(self, item: dict) -> str:
        method = item["method"]
        task = self._jinja_render(
            self.agent_prompts["task_template"],
            class_name=item["class_name"],
            file_path=item["file_path"],
            method_declaration=method["declaration"],
            method_description=method["description"],
        )
        hint_level = item.get("hint_level", 0)
        hint = self._get_hint(item)
        if hint and hint_level == 4:
            task += "\n\n" + (f"You're given a full solution:\n```python\n{hint}```\n"
                              "This is full solution, use it to produce the final answer.")
        elif hint:
            task += "\n\n" + (f"You're given a part of the method:\n```python\n{hint}```\n"
                              "This is part of the solution, use it to produce the final answer.")
        return self._jinja_render(
            self.agent_prompts["instance_template"],
            task=task,
            **_CONTAINER_SYSTEM_INFO,
        )

    def _render_observation(self, result: dict[str, Any]) -> str:
        cmd_output = result.get("command_output", {})
        output = extract_bash_output(cmd_output)
        out_obj = {
            "returncode": int(cmd_output.get("exit_code", 0)),
            "output": output,
            "exception_info": None,
        }
        return self._jinja_render(
            self.agent_prompts["observation_template"],
            output=out_obj,
            max_output_length=self.max_test_output_symb,
            head_tail_length=self.max_test_output_symb // 2,
        )

    def _generate_messages(self, dp_item: dict) -> list[dict]:
        system_prompt = self._jinja_render(self.agent_prompts["system_template"])
        user_prompt = self._render_instance_prompt(dp_item)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    # --- ItemToRun builder ---

    def build_item(self, item: dict, code: str = "") -> ItemToRun:
        method = item["method"]
        if "_num_decorators" not in method:
            def_line_idx = method["global_method_declaration_index"][0]
            file_lines = item["raw_file_content"].splitlines()
            method["_num_decorators"] = count_decorators_above(file_lines, def_line_idx)
        startline = method["global_method_declaration_index"][0] + 1 - method["_num_decorators"]
        endline = method["global_method_body_index"][1] + 1
        tests = item["tests"]["full_paths"]
        if tests and self.max_num_tests > 0:
            tests = tests[:self.max_num_tests]
        return ItemToRun(
            idx=item["idx"],
            dp_id=item["dp_id"],
            file_path=item["file_path"],
            replace_content=code,
            method_name=method["name"],
            start_line=startline,
            end_line=endline,
            tests=tests,
        )

    # --- Initialize ---

    async def _initialize(self, state: dict) -> None:
        """Create IDEGym server and cut the target method."""
        init_start_time = time.perf_counter()

        # Initialize state defaults
        state["responses_raw"] = []
        state["test_results"] = []
        state["test_status"] = None
        state["generated_code_blocks"] = []
        state["turn"] = 0
        state["should_continue"] = False
        state["stop_reason"] = None
        state["edited_file"] = []
        state["no_code_block"] = None
        state["test_passed"] = None
        state["test_output"] = None
        state["tests_passed_percentage"] = None
        state["turn_exit"] = None
        state["timestamp_start"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["timestamp_end"] = None
        state["duration"] = None
        state["turn_tests_durations"] = []
        state["turn_gen_durations"] = []
        state["reward_components"] = None
        state["rm_score"] = FAILED_PIPELINE_SCORE
        state["client"] = None
        state["server"] = None
        state["server_id"] = None
        state["server_init_failed"] = None
        state["is_failed_rollout"] = False
        state["trajectory"] = []
        state["metadata"] = None
        state["submit_mode"] = None

        item = state.get("dp_item")
        if item is None:
            state["is_failed_rollout"] = True
            state["stop_reason"] = "no_dp_item"
            return

        dp_id = item["dp_id"]

        # Generate initial messages
        state["messages"] = self._generate_messages(item)

        # Create client
        try:
            client = await self.idegym_runner.create_client()
            state["client"] = client
        except Exception as e:
            logger.error(f"[INIT] dp_id={dp_id} - Failed to create client: {e}")
            state["is_failed_rollout"] = True
            state["stop_reason"] = "initialization_error"
            state["server_init_failed"] = 1.0
            return

        # Create server
        try:
            server = await self.idegym_runner.create_server(client)
            state["server"] = server
            state["server_id"] = server.server_id
        except Exception as e:
            logger.error(f"[INIT] dp_id={dp_id} - Failed to create server: {e}")
            try:
                await self.idegym_runner.close_client(client)
            except Exception:
                pass
            state["client"] = None
            state["is_failed_rollout"] = True
            state["stop_reason"] = "initialization_error"
            state["server_init_failed"] = 1.0
            return

        # Cut the target method
        item_to_cut = self.build_item(item, code="")
        try:
            result = await self.idegym_runner.edit_file(server, item_to_cut)
            self._record(state, "edit_file", result, item=item_to_cut.to_dict(), dp_id=dp_id)
        except Exception as e:
            logger.error(f"[INIT] dp_id={dp_id} - Failed to cut method: {e}")
            await self.idegym_runner.finish_server(server)
            await self.idegym_runner.close_client(client)
            state["client"] = None
            state["server"] = None
            state["is_failed_rollout"] = True
            state["stop_reason"] = "initialization_error"
            state["server_init_failed"] = 1.0
            return

        state["should_continue"] = True
        logger.info(f"[INIT] dp_id={dp_id} - Initialize completed in {time.perf_counter() - init_start_time:.2f}s")

    # --- Agent step ---

    async def _agent_step(self, state: dict, sampling_params: dict) -> None:
        """One iteration of the agent loop: generate, parse, execute."""
        state["turn"] += 1
        step = state["turn"]

        if step > self.max_turns:
            state["stop_reason"] = "max_turns_reached"
            state["should_continue"] = False
            return

        # Generate
        start_time = time.perf_counter()
        content, function_calls = await self._call_model(state, sampling_params)
        state["turn_gen_durations"].append(time.perf_counter() - start_time)

        if content is None:
            # Generation failed, _call_model already set is_failed_rollout
            return

        # Apply reasoning filter
        if self.keep_reasoning == "none":
            content = apply_reasoning_filter(content, self.max_turns)

        # Store raw response
        state["responses_raw"].append({"content": content, "function_calls": function_calls})

        # Parse actions
        try:
            actions = self._strategy.parse_actions(content, function_calls)
        except FormatError as e:
            if step < self.max_turns:
                if e.messages:
                    state["messages"].append({"role": "user", "content": e.messages[0]["content"]})
                else:
                    state["messages"].append({"role": "user", "content": "Format error occurred, please try again."})
                # Encode observation and append to prompt
                await self._append_observation_tokens(state, state["messages"][-1:])
            state["should_continue"] = True
            return
        except Exception:
            traceback.print_exc()
            raise

        # Execute commands
        commands = [a["command"] for a in actions]
        observations, stop = await self._execute_commands(commands, state)

        if stop:
            state["should_continue"] = False
            return

        # Build observation messages and append to conversation
        obs_msgs = self._strategy.build_observation_msgs(actions, observations)
        state["messages"].extend(obs_msgs)
        await self._append_observation_tokens(state, obs_msgs)
        state["should_continue"] = True

    async def _call_model(self, state: dict, sampling_params: dict) -> tuple[str | None, list]:
        """Call verl LLM server. Returns (content, function_calls) or (None, []) on failure."""
        messages = state["messages"]
        tools = self._strategy.get_tools() or None

        # Tokenize: initial turn encodes full history, follow-up appends observation tokens
        if "prompt_ids" not in state:
            # First call: encode full message history
            prompt_ids = await self.apply_chat_template(messages, tools=tools)
            state["prompt_ids"] = prompt_ids
            state["response_mask"] = []
            state["request_id"] = uuid.uuid4().hex

        prompt_ids = state["prompt_ids"]
        request_id = state["request_id"]

        # Stop early if no response budget remains (vLLM requires max_tokens >= 1)
        if len(state["response_mask"]) >= self.rollout_config.response_length:
            state["should_continue"] = False
            state["stop_reason"] = "max_response_length"
            return None, []

        try:
            output = await self.server_manager.generate(
                request_id=request_id,
                prompt_ids=prompt_ids,
                sampling_params=sampling_params,
            )
        except Exception as e:
            logger.error(f"Generation failed: {e}")
            state["should_continue"] = False
            state["is_failed_rollout"] = True
            return None, []

        response_ids = output.token_ids

        # Accumulate trajectory
        state["prompt_ids"] = prompt_ids + response_ids
        state["response_mask"] = state["response_mask"] + [1] * len(response_ids)

        # Check response length limit
        if len(state["response_mask"]) >= self.rollout_config.response_length:
            state["should_continue"] = False
            state["stop_reason"] = "max_response_length"

        # Decode response
        content, function_calls = await self._tool_parser.extract_tool_calls(response_ids)

        # Add assistant message to conversation history
        assistant_msg = {"role": "assistant", "content": content}
        if function_calls:
            assistant_msg["tool_calls"] = [
                {"name": fc.name, "args": json.loads(fc.arguments)} for fc in function_calls
            ]
        state["messages"].append(assistant_msg)

        return content, function_calls

    async def _append_observation_tokens(self, state: dict, obs_msgs: list[dict]) -> None:
        """Encode observation messages and append to prompt_ids with mask=0."""
        if not obs_msgs:
            return
        obs_ids = await self.apply_chat_template(obs_msgs, remove_system_prompt=True)
        state["prompt_ids"] = state["prompt_ids"] + obs_ids
        state["response_mask"] = state["response_mask"] + [0] * len(obs_ids)

    async def _execute_commands(self, commands: list[str], state: dict) -> tuple[list[str], bool]:
        """Execute bash commands sequentially via IDEGym."""
        server = state["server"]
        step = state["turn"]
        observations: list[str] = []
        dp_id = state["dp_item"]["dp_id"]

        for cmd_idx, command in enumerate(commands):
            has_submit = SUBMIT_SIGNAL in command

            if has_submit and command.strip() == SUBMIT_SIGNAL:
                state["stop_reason"] = "submitted"
                state["submit_mode"] = "standalone"
                return observations, True

            try:
                result = await self.idegym_runner.run_bash(server, command)
                self._record(state, "run_bash", result, command=command, step=step, cmd_idx=cmd_idx, dp_id=dp_id)
                observations.append(self._render_observation(result))
            except Exception as e:
                exc_str = str(e).lower()
                if "idegym" in exc_str or "timeout" in exc_str:
                    state["stop_reason"] = "run_bash_error"
                    state["test_status"] = "CRUSHED"
                    state["should_continue"] = False
                    state["is_failed_rollout"] = True
                    return observations, True
                error_msg = f"Command execution failed: {type(e).__name__}: {e}"
                tpl = self.agent_prompts["format_error_template"]
                observations.append(self._jinja_render(tpl, error=error_msg, actions=[]))
                continue

            if has_submit:
                state["stop_reason"] = "submitted"
                state["submit_mode"] = "combined"
                return observations, True

        return observations, False

    # --- Run tests ---

    async def _run_tests(self, state: dict) -> None:
        """Run tests on the server after agent submission."""
        item = state["dp_item"]
        dp_id = item["dp_id"]
        server = state["server"]

        if state["test_status"] == "CRUSHED":
            return

        item_to_test = self.build_item(item, code="")
        try:
            test_start = time.perf_counter()
            raw_result = await self.idegym_runner.run_tests(server=server, item=item_to_test, do_edit=False)
            test_duration = time.perf_counter() - test_start
            logger.info(f"[TESTS] dp_id={dp_id} - Tests completed in {test_duration:.2f}s")

            self._record(state, "run_item", raw_result, item=item_to_test.to_dict(), dp_id=dp_id)

            test_result = self._parse_test_result(raw_result)
            state["test_results"] = [test_result]
            state["test_status"] = test_result["status"]
        except Exception as e:
            logger.error(f"[TESTS] dp_id={dp_id} - Error running tests: {e}")
            traceback.print_exc()
            state["test_status"] = "CRUSHED"

    def _parse_test_result(self, raw_result: dict[str, Any]) -> dict[str, Any]:
        test_output = raw_result.get("test_output", "")
        raw_result.pop("datapoint", None)
        parsed = parse_idegym_tests_output(test_output)
        raw_result["summary"] = parsed.pop("summary")
        raw_result["details"] = parsed.get("tests", [])
        if raw_result["summary"] is not None:
            raw_result["status"] = raw_result["summary"]["status"]
        else:
            raw_result["status"] = "FAILED"
        raw_result["percentage_passed"] = get_percentage_passed(raw_result)
        raw_result["test_output"] = test_output
        return raw_result

    # --- Finalize ---

    def _calculate_reward_score(self, state: dict) -> dict:
        test_results = state.get("test_results", [])
        test_result = test_results[-1] if test_results else {}
        percentage_passed = test_result.get("percentage_passed", 0.0)
        return {
            "percentage_passed": percentage_passed,
            "efficiency": 0.0,
            "score": percentage_passed,
        }

    async def _finalize(self, state: dict) -> None:
        """Stop server, compute reward, save trajectory."""
        server = state.get("server")
        client = state.get("client")
        dp_id = state["dp_item"]["dp_id"] if state.get("dp_item") else "unknown"

        if server is None and client is None:
            return

        state["timestamp_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if state.get("timestamp_start"):
            start = datetime.strptime(state["timestamp_start"], "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(state["timestamp_end"], "%Y-%m-%d %H:%M:%S")
            state["duration"] = (end - start).total_seconds()

        # Calculate reward
        reward_components = self._calculate_reward_score(state)
        state["rm_score"] = reward_components["score"]
        state["reward_components"] = reward_components
        state["tests_passed_percentage"] = reward_components["percentage_passed"]

        # Stop server
        if server:
            await self.idegym_runner.finish_server(server)
            state["server"] = None

        # Close client
        if client:
            try:
                await self.idegym_runner.close_client(client)
            except Exception as e:
                logger.error(f"[FINALIZE] dp_id={dp_id} - Error closing client: {e}")
            state["client"] = None

        # Save metadata
        dp_item = state["dp_item"]
        state["dp_item"] = None
        state["metadata"] = {
            "difficulty": dp_item.get("difficulty") if dp_item else None,
            "dp_id": dp_item.get("dp_id") if dp_item else None,
            "dp_idx": dp_item.get("idx") if dp_item else None,
        }
        state.pop("prompt_ids")
        state.pop("response_mask")
        state.pop("request_id")

        # Save trajectory to local file
        if self.traj_local_path:
            try:
                with open(self.traj_local_path, "a", encoding="utf-8") as f:
                    fcntl.flock(f, fcntl.LOCK_EX)
                    try:
                        json.dump(state, f, default=str)
                        f.write("\n")
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
            except Exception as e:
                logger.error(f"[FINALIZE] dp_id={dp_id} - Failed to save trajectory locally: {e}")

    # --- Trajectory recording ---

    def _record(self, state: dict, action: str, result: dict | None, **extra: Any) -> None:
        server_id = -1
        result_keys = []
        success = False
        if result is not None and isinstance(result, dict):
            server_id = result.get("server_id")
            result_keys = list(result.keys())
            success = True
        state["trajectory"].append({
            "action": action,
            "timestamp": time.time(),
            "server_id": server_id,
            "success": success,
            **extra,
            "result_keys": result_keys,
        })

    # --- verl AgentLoopBase interface ---

    async def run(self, sampling_params: dict[str, Any], **kwargs) -> AgentLoopOutput:
        messages = list(kwargs["raw_prompt"])
        dp_item = kwargs.get("dp_item") or kwargs.get("tools_kwargs", {}).get("item")

        state: dict[str, Any] = {"dp_item": dp_item, "messages": messages}

        # Initialize
        await self._initialize(state)

        # Agent loop
        while state["should_continue"]:
            await self._agent_step(state, sampling_params)

        # Run tests
        if state.get("dp_item") and not state.get("is_failed_rollout"):
            await self._run_tests(state)

        # Finalize
        await self._finalize(state)

        # Build output
        return self._build_output(state)

    def _build_output(self, state: dict) -> AgentLoopOutput:
        """Convert state to AgentLoopOutput for verl training."""
        response_length = self.rollout_config.response_length

        prompt_ids = state.get("prompt_ids", [0, 0])
        response_mask = state.get("response_mask", [0])

        if not response_mask:
            # No generation happened (failed rollout) — use dummy tokens
            prompt_ids = [0, 0]
            response_mask = [0]

        # Split prompt_ids into prompt and response parts using response_mask
        response_ids = prompt_ids[-len(response_mask):]
        prompt_only_ids = prompt_ids[:len(prompt_ids) - len(response_mask)]

        # Count turns from messages
        num_turns = 0
        prev_role = None
        for msg in state.get("messages", []):
            role = msg.get("role") if isinstance(msg, dict) else "unknown"
            if role == "system":
                continue
            if role != prev_role:
                num_turns += 1
                prev_role = role

        output = AgentLoopOutput(
            prompt_ids=prompt_only_ids,
            response_ids=response_ids[:response_length],
            response_mask=response_mask[:response_length],
            num_turns=num_turns,
            metrics={},
        )

        # Propagate reward
        output.reward_score = float(state["rm_score"]) if state.get("rm_score") is not None else None
        default_reward_info = {"percentage_passed": 0.0, "efficiency": 0.0, "score": 0.0}
        if state.get("reward_components"):
            reward_info = dict(state["reward_components"])
            reward_info.setdefault("percentage_passed", 0.0)
            reward_info.setdefault("score", 0.0)
        else:
            reward_info = default_reward_info
        output.extra_fields["reward_extra_info"] = reward_info

        return output
