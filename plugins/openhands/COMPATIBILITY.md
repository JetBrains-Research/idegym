# Compatibility

Validate all details against the pinned versions before shipping. Every version-specific OpenHands
import and construction detail lives in a single module: `src/idegym/plugins/openhands/runtime/compat.py`.
The `unit-tests/test_openhands_compat.py` suite re-verifies these paths wherever OpenHands is
installed; run it with `plugins/openhands/run-compat-tests.sh`.

## Pinned versions

| Component            | Pin       | Notes |
|----------------------|-----------|-------|
| `openhands-sdk`      | `1.36.0`  | pulls `fastmcp>=3`, `litellm`, `pydantic>=2.12.5`, `lmnr` |
| `openhands-tools`    | `1.36.0`  | pulls `libtmux`, `browser-use`, `tree-sitter*`, `func-timeout` |
| FastMCP (service)    | `>=3`     | IdeGYM already ships `fastmcp 3.3.1` |
| MCP                  | via FastMCP | |

Bump `PINNED_OPENHANDS_SDK` / `PINNED_OPENHANDS_TOOLS` in `runtime/compat.py` **and** the image
plugin defaults together, then run the compatibility suite.

## Why OpenHands runs in its own environment

`openhands-sdk` transitively pins `opentelemetry-api==1.39.1` (through `lmnr`), while
`idegym-backend-utils` requires `opentelemetry-api>=1.43.0`. These are mutually exclusive, so
OpenHands **cannot** be installed into the IdeGYM environment, and `openhands-sdk`/`openhands-tools`
are deliberately absent from the workspace lock.

Consequences:

- The OpenHands Tools Service runs in its **own in-container virtualenv** (created by the image
  plugin), isolated from the IdeGYM server. IdeGYM reaches it over loopback HTTP.
- The compatibility suite runs in a **separate virtualenv** via `plugins/openhands/run-compat-tests.sh`
  (the same isolation the service uses). It is skipped in the main `pytest` run because OpenHands is
  not importable there.
- `browser-use`, `litellm`, and the rest of the OpenHands tree never touch the monorepo lock.

## Verified OpenHands API surface (1.36.0)

| Purpose | Symbol | Signature (verified) |
|---|---|---|
| Terminal factory (single retained session) | `openhands.tools.terminal.create_terminal_session` | `(work_dir, username=None, no_change_timeout_seconds=None, terminal_type=None\|"tmux"\|"subprocess"\|"powershell", shell_path=None, env=None) -> TerminalSession` |
| Terminal action | `TerminalAction` | `command: str, is_input=False, timeout: float\|None=None, reset=False` |
| Terminal observation | `TerminalObservation` | `content: list[TextContent\|ImageContent], is_error, command, exit_code: int\|None, timeout: bool, metadata (working_dir, exit_code, …)` |
| Running detection | — | `exit_code is None or exit_code < 0` ⇒ running / no-change timeout; `>= 0` ⇒ completed |
| Session methods | `TerminalSession` | `.initialize()`, `.execute(action)`, `.interrupt() -> bool`, `.close()`, `.cwd` |
| Tool construction | `<Family>Tool.create(conv_state)` | reads only `conv_state.workspace.working_dir` / `.persistence_dir` / `.env_observation_persistence_dir` (a narrow, agent-free shim). `file_editor` also reads `agent.llm.vision_is_active()`, so it is built directly instead |
| Argument validation | `ToolDefinition.action_from_arguments(dict) -> Action` | |
| Agentless dispatch | `ToolDefinition.acall(action, conversation=None) -> Observation` | `conversation=None` ⇒ no agent/LLM |
| Schema (REST + MCP) | `ToolDefinition.to_mcp_tool() -> {name, description, inputSchema, outputSchema, annotations}` | single source for both surfaces |

`create_terminal_session` returns a single `TerminalSession` — the `TmuxPanePool` logic lives only in
`TerminalExecutor`, which the plugin avoids — so a terminal handle keeps stable pane/process affinity.

## Tool classification

| Family / tool | Status | Treatment |
|---|---|---|
| `terminal` | **enabled** | dual tmux/subprocess backends via `create_terminal_session`; one pinned session per handle |
| `file_editor` | **enabled** | executor + definition built directly (no agent) |
| `apply_patch` | **enabled** | `create(shim)`; workspace mutation lock |
| `grep`, `glob` | **enabled** | `create(shim)`; read-only, parallel-safe |
| `planning_file_editor` | **enabled** | `create(shim)` |
| `task_tracker` | **enabled** | `create(shim)`; service-owned persistence dir |
| Gemini `read_file` / `write_file` / `edit` / `list_directory` | **enabled** | `create(shim)`; canonical names preserved |
| `browser_use` | **enabled in `full`** | classified; adapter deferred → `missing_dependency` until the browser runtime is wired |
| `task` | **unsupported** | `requires_agent` — subagent execution |
| `workflow` | **unsupported** | `requires_agent` — dynamic workflow code |
| `tom_consult` | **unsupported** | `requires_agent` — consultation agent/LLM |
| `delegate` | **not a callable tool** | delegation models/visualization only |
| `preset`, `utils` | **not a callable tool** | configuration / internal support packages |

The catalog audit test fails if the set of installed `openhands.tools` submodules diverges from
`compat.KNOWN_TOOL_FAMILIES`, so a new upstream family cannot be silently ignored.

## Scope of this build

- The browser (`full`) profile, tmux service-restart reattachment, and quota/metrics hardening are
  deferred; each is classified and documented rather than faked.
- Image builds and Kubernetes end-to-end tests run in CI. The build-context assets are staged by the
  local build driver via `get_context_files`; the Kaniko git-context path resolves the same
  `plugins/openhands/scripts/...` paths against a checkout of this repository.
