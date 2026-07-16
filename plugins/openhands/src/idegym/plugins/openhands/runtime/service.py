"""ToolRuntime — the authoritative runtime shared by REST, MCP, and the client.

It owns the tool catalog, the OpenHands-backed adapters, the terminal session manager, the resource
scheduler, the artifact store, the deduplication cache, and the environment generation. REST and MCP
are thin projections over this single object; neither builds its own executors or terminals.
"""

import base64
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import (
    CallStatus,
    CapabilityResponse,
    ContentBlock,
    ContentType,
    Diagnostics,
    HealthResponse,
    Profile,
    ResetResponse,
    SupportStatus,
    TerminalCreateRequest,
    TerminalDescriptor,
    TerminalResult,
    ToolCallResult,
    ToolDescriptor,
    ToolSchemaResponse,
)
from idegym.plugins.openhands.api.names import ToolFamily, ToolName
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.adapters.openhands import OpenHandsToolAdapter
from idegym.plugins.openhands.runtime.artifacts import ArtifactStore
from idegym.plugins.openhands.runtime.catalog import ADAPTER_FAMILIES, CatalogEntry, LockScope, ToolCatalog
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.dedup import RequestDeduplicator, canonical_hash
from idegym.plugins.openhands.runtime.scheduler import ResourceScheduler
from idegym.plugins.openhands.runtime.terminal.manager import TerminalSessionManager

_PATH_KEYS = ("path", "file_path", "file", "abs_path", "absolute_path")
# Every caller-controlled filesystem path key across the file/search tools. Validated + canonicalized
# for any tool flagged ``filesystem`` in the catalog, independent of lock policy.
_FS_PATH_KEYS = ("path", "file_path", "file", "abs_path", "dir_path", "directory")
# Glob metacharacters that begin the variable part of a pattern; the leading non-magic prefix is a
# real path whose search root must be inside the workspace.
_GLOB_MAGIC = ("*", "?", "[")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_call_id() -> str:
    return uuid.uuid4().hex


class ToolRuntime:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self._workspace_base = Path(config.workspace_root).resolve()
        self.catalog = ToolCatalog(config.profile, config.enabled_tools, config.disabled_tools)
        self.scheduler = ResourceScheduler()
        self.artifacts = ArtifactStore(
            config.output_dir,
            max_artifacts=config.max_artifacts,
            max_total_bytes=config.max_artifact_bytes,
            max_single_bytes=config.max_single_artifact_bytes,
        )
        self._environment_generation = 0
        self._environment_id = uuid.uuid4().hex
        self.terminals = TerminalSessionManager(config, lambda: self._environment_id)
        self._adapters: dict[str, OpenHandsToolAdapter] = {}
        self._adapter_errors: dict[str, str] = {}
        self._dedup = RequestDeduplicator(config.dedup_cache_size)
        self._prepared = False
        self._ready = False

    # -- lifecycle --------------------------------------

    def prepare(self) -> None:
        """Synchronous startup: directories, backend probe, and adapter construction.

        Called before the MCP server is built so the MCP tool list reflects the enabled catalog
        (the async ``start`` only adds the eager default terminal and flips readiness).
        """
        if self._prepared:
            return
        self.config.ensure_directories()
        # Discard artifacts orphaned by a previous process: their metadata lived only in memory, so
        # they are unreachable through the API and untracked by quota/eviction after a restart.
        self.artifacts.purge_storage()
        self.terminals.probe_backends()
        self._build_adapters()
        self._prepared = True

    async def start(self) -> None:
        self.prepare()
        if (
            self.terminal_enabled()
            and self.config.auto_create_default_terminal
            and self.terminals.default_backend_ready()
        ):
            await self.terminals.ensure_default()
        self._ready = True

    async def stop(self) -> None:
        self._ready = False
        await self.terminals.reset_all()

    def _build_adapters(self) -> None:
        self._adapters.clear()
        self._adapter_errors.clear()
        if not compat.openhands_available():
            return
        for family in ADAPTER_FAMILIES:
            persistence = os.path.join(self.config.state_dir, family.value)
            env_persistence = os.path.join(self.config.state_dir, f"{family.value}-env")
            try:
                tools = compat.build_family_tools(
                    family.value,
                    working_dir=self.config.workspace_root,
                    persistence_dir=persistence,
                    env_persistence_dir=env_persistence,
                )
            except Exception as ex:  # pragma: no cover - only with OpenHands present
                self._adapter_errors[family.value] = str(ex)
                continue
            for tool in tools:
                adapter = OpenHandsToolAdapter(family.value, tool)
                self._adapters[adapter.name] = adapter

    # -- discovery ----------------------------------------------------------

    def _browser_available(self) -> bool:
        return self.config.browser_enabled and self.config.profile == Profile.FULL

    def _resolved_status(self, entry: CatalogEntry) -> SupportStatus:
        """Resolve status from the actually-callable set, not just package/profile booleans.

        A tool is only ENABLED if there is a real callable path: the terminal via its backend, an
        adapter that actually built for a filesystem/search tool. Browser has no adapter/route/MCP
        tool yet, so it is never advertised callable; an adapter that failed to build is reported
        ADAPTER_INCOMPATIBLE rather than ENABLED.
        """
        status = self.catalog.effective_status(
            entry,
            openhands_available=compat.openhands_available(),
            browser_available=self._browser_available(),
        )
        if status != SupportStatus.ENABLED:
            return status
        if entry.is_terminal:
            return status
        if entry.family == ToolFamily.BROWSER:
            # No browser adapter, REST route, or MCP tool is implemented yet — keep it non-callable.
            return SupportStatus.MISSING_DEPENDENCY
        if entry.name not in self._adapters:
            # openhands present and the tool is in-profile, but its adapter did not construct.
            return SupportStatus.ADAPTER_INCOMPATIBLE
        return SupportStatus.ENABLED

    def list_capabilities(self) -> list:
        out = []
        for entry in self.catalog.entries():
            status = self._resolved_status(entry)
            cap = self.catalog.capability(entry, status)
            if status == SupportStatus.ADAPTER_INCOMPATIBLE:
                err = self._adapter_errors.get(entry.family.value)
                cap.reason = f"Adapter failed to build: {err}" if err else "Tool adapter is unavailable in this build"
            out.append(cap)
        return out

    def diagnostics(self) -> Diagnostics:
        browser = self._browser_available()
        summary: dict[str, int] = {}
        for cap in self.list_capabilities():
            summary[cap.status.value] = summary.get(cap.status.value, 0) + 1
        versions = compat.openhands_versions()
        return Diagnostics(
            plugin_version=_plugin_version(),
            openhands_sdk_version=versions.get("openhands-sdk"),
            openhands_tools_version=versions.get("openhands-tools"),
            profile=self.config.profile,
            catalog_summary=summary,
            browser_available=browser,
            adapter_errors=dict(self._adapter_errors),
            workspace_root=self.config.workspace_root,
            state_dir=self.config.state_dir,
            output_dir=self.config.output_dir,
            environment_generation=self._environment_generation,
        )

    def capability_response(self) -> CapabilityResponse:
        return CapabilityResponse(
            profile=self.config.profile,
            capabilities=self.list_capabilities(),
            backends=self.terminals.backend_config_view(),
            diagnostics=self.diagnostics(),
        )

    def _enabled_entries(self) -> list[CatalogEntry]:
        return [e for e in self.catalog.entries() if self._resolved_status(e) == SupportStatus.ENABLED]

    def list_tools(self) -> list[ToolDescriptor]:
        out: list[ToolDescriptor] = []
        for entry in self._enabled_entries():
            if entry.is_terminal:
                out.append(self._terminal_descriptor())
                continue
            adapter = self._adapters.get(entry.name)
            if adapter is None:
                continue
            out.append(
                ToolDescriptor(
                    name=adapter.name,
                    family=entry.family.value,
                    description=adapter.description,
                    input_schema=adapter.input_schema,
                    output_schema=adapter.output_schema,
                    annotations=adapter.annotations,
                )
            )
        return out

    def _terminal_descriptor(self) -> ToolDescriptor:
        from idegym.plugins.openhands.api.models import TerminalExecuteRequest

        schema = TerminalExecuteRequest.model_json_schema()
        return ToolDescriptor(
            name=ToolName.TERMINAL,
            family="terminal",
            description=(
                "Run a shell command in a stateful terminal (default terminal unless a terminal_id "
                "is supplied). Backed by an OpenHands terminal session."
            ),
            input_schema=schema,
            annotations={"destructive": True, "open_world": True},
        )

    def get_tool_schema(self, name: str) -> ToolSchemaResponse:
        try:
            entry = self.catalog.get(name)
        except KeyError:
            raise ServiceError(ErrorCode.UNKNOWN_TOOL, f"Unknown tool: {name}")
        if entry.is_terminal:
            d = self._terminal_descriptor()
            return ToolSchemaResponse(
                name=d.name,
                family=d.family,
                description=d.description,
                input_schema=d.input_schema,
                annotations=d.annotations,
            )
        adapter = self._adapters.get(name)
        if adapter is None:
            raise ServiceError(ErrorCode.TOOL_DISABLED, f"Tool {name} is not currently callable")
        return ToolSchemaResponse(
            name=adapter.name,
            family=entry.family.value,
            description=adapter.description,
            input_schema=adapter.input_schema,
            output_schema=adapter.output_schema,
            annotations=adapter.annotations,
        )

    # -- health -------------------------------------------------------------

    def health(self) -> HealthResponse:
        workspace_ok = Path(self.config.workspace_root).is_dir()
        # Readiness must not require a terminal backend when the terminal tool is disabled.
        backend_ok = (not self.terminal_enabled()) or self.terminals.default_backend_ready()
        # An in-profile tool whose adapter failed to construct fails readiness (the operator asked
        # for it, but it is not callable).
        adapters_ok = not any(
            self._resolved_status(e) == SupportStatus.ADAPTER_INCOMPATIBLE for e in self.catalog.entries()
        )
        checks = {
            "workspace": workspace_ok,
            "default_backend": backend_ok,
            "adapters": adapters_ok,
            "catalog": len(self.catalog.entries()) > 0,
            "artifacts_dir": Path(self.config.output_dir).is_dir(),
        }
        ready = self._ready and all(checks.values())
        return HealthResponse(
            live=True,
            ready=ready,
            checks=checks,
            backends=self.terminals.backend_config_view(),
            environment_generation=self._environment_generation,
        )

    # -- tool dispatch -----------------------------------

    def _status_for(self, entry: CatalogEntry) -> SupportStatus:
        return self._resolved_status(entry)

    def _ensure_enabled(self, entry: CatalogEntry) -> None:
        status = self._status_for(entry)
        if status != SupportStatus.ENABLED:
            self._raise_for_status(entry, status)

    def terminal_enabled(self) -> bool:
        """True when the terminal tool is enabled by the active profile/overrides."""
        try:
            entry = self.catalog.get(ToolName.TERMINAL)
        except KeyError:
            return False
        return self._status_for(entry) == SupportStatus.ENABLED

    def ensure_terminal_enabled(self) -> None:
        """Raise unless the terminal tool is enabled. Guards every terminal lifecycle path."""
        self._ensure_enabled(self.catalog.get(ToolName.TERMINAL))

    def _lock_requests(self, entry: CatalogEntry, arguments: dict[str, Any]) -> list[tuple[str, bool]]:
        """Map a tool call to reader/writer lock requests reflecting the resources it touches.

        Hierarchy: a workspace mutation (apply_patch) takes the workspace key *exclusively* so it
        conflicts with every contained file operation; file operations take the workspace *shared*
        plus an exclusive per-file lock, so same-file read/write serialize while unrelated file
        operations run in parallel. glob's fallback uses process-global ``os.chdir`` and is not
        parallel-safe, so it takes a tool-wide exclusive lock.
        """
        ws = f"workspace:{self.config.workspace_root}"
        scope = entry.lock_scope
        if scope == LockScope.WORKSPACE:
            return [(ws, True)]
        if scope == LockScope.PATH:
            path = self._extract_path(arguments)
            file_key = f"file:{path}" if path else f"tool:{entry.name}"
            return [(ws, False), (file_key, True)]
        if scope == LockScope.NONE:
            # Read/search tools: a shared workspace lease so a mutation still excludes them.
            reqs: list[tuple[str, bool]] = [(ws, False)]
            if entry.name == ToolName.READ_FILE:
                # read_file declares a file resource: a read must not overlap a write to that file.
                path = self._extract_path(arguments)
                if path:
                    reqs.append((f"file:{path}", True))
            elif entry.name == ToolName.GLOB:
                reqs.append((f"tool:{entry.name}", True))
            return reqs
        return [(f"tool:{entry.name}", True)]

    def _extract_path(self, arguments: dict[str, Any]) -> Optional[str]:
        for key in _PATH_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return self._normalize_path(value)
        return None

    def _normalize_path(self, raw: str) -> str:
        base = self._workspace_base
        candidate = Path(raw)
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
        if self.config.enforce_workspace_boundary and base != resolved and base not in resolved.parents:
            raise ServiceError(
                ErrorCode.PATH_OUTSIDE_WORKSPACE,
                f"Path {raw!r} resolves outside the workspace root",
                {"workspace_root": str(base)},
            )
        return str(resolved)

    def _validate_action_paths(self, entry: CatalogEntry, arguments: dict[str, Any]) -> dict[str, Any]:
        """Enforce the workspace boundary for every filesystem path an action carries.

        Runs for any ``filesystem`` tool regardless of lock policy — a read/search tool with
        ``LockScope.NONE`` (grep/glob/read_file/list_directory) must still be confined. Each path
        field is canonicalized (``Path.resolve`` resolves symlinks, so a symlink escape is rejected)
        and the sanitized canonical paths are what reach the executor.
        """
        if not entry.filesystem or not self.config.enforce_workspace_boundary:
            return arguments
        sanitized = dict(arguments)
        for key in _FS_PATH_KEYS:
            value = sanitized.get(key)
            if isinstance(value, str) and value:
                sanitized[key] = self._normalize_path(value)
        if entry.name == ToolName.GLOB:
            pattern = sanitized.get("pattern")
            if isinstance(pattern, str) and pattern:
                self._validate_glob_pattern(pattern)
        return sanitized

    def _validate_glob_pattern(self, pattern: str) -> None:
        """Reject a glob whose non-magic search root escapes the workspace (abs or ``../``)."""
        cut = len(pattern)
        for i, ch in enumerate(pattern):
            if ch in _GLOB_MAGIC:
                cut = i
                break
        prefix = pattern[:cut]
        base = prefix if prefix.endswith("/") else os.path.dirname(prefix)
        if base:
            self._normalize_path(base)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        terminal_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> ToolCallResult:
        # Validate the operation (tool exists + enabled) BEFORE consulting the dedup cache, so a
        # reused id can never bypass validation or return another tool's cached result.
        try:
            entry = self.catalog.get(name)
        except KeyError:
            raise ServiceError(ErrorCode.UNKNOWN_TOOL, f"Unknown tool: {name}")
        self._ensure_enabled(entry)

        async def _dispatch() -> ToolCallResult:
            if entry.is_terminal:
                result = await self._call_terminal_tool(arguments, terminal_id)
            else:
                result = await self._call_adapter_tool(entry, arguments)
            # One total response budget across content text, images, and structured data — applied to
            # every path (terminal included), so no result can hold hundreds of MB in memory.
            return self._bound_result(result)

        if not request_id:
            return await _dispatch()

        # Canonical dedup key: operation kind + tool + terminal + environment generation + args.
        body_hash = canonical_hash(
            {
                "kind": "call_tool",
                "tool": name,
                "terminal_id": terminal_id,
                "generation": self._environment_generation,
                "arguments": arguments,
            }
        )
        return await self._dedup.run(request_id, body_hash, _dispatch)

    async def _call_terminal_tool(self, arguments: dict[str, Any], terminal_id: Optional[str]) -> ToolCallResult:
        command = arguments.get("command")
        if not isinstance(command, str):
            raise ServiceError(ErrorCode.INVALID_ARGUMENTS, "terminal requires a string 'command'")
        if terminal_id is None:
            handle = await self.terminals.ensure_default()
            terminal_id = handle.terminal_id
        call_id = _new_call_id()
        term = await self.terminals.execute(
            terminal_id,
            command,
            timeout=arguments.get("timeout"),
            is_input=bool(arguments.get("is_input", False)),
            reset=bool(arguments.get("reset", False)),
            call_id=call_id,
        )
        # Strip the fields that duplicate the (bounded) content blocks so the structured payload does
        # not carry a second, unbounded copy of the terminal output.
        structured = term.model_dump(mode="json")
        structured.pop("output", None)
        structured.pop("content", None)
        return ToolCallResult(
            call_id=call_id,
            tool=ToolName.TERMINAL,
            status=term.status,
            is_error=term.is_error,
            content=term.content,
            structured=structured,
            metadata=term.metadata,
            started_at=term.started_at,
            finished_at=term.finished_at,
        )

    async def _call_adapter_tool(self, entry: CatalogEntry, arguments: dict[str, Any]) -> ToolCallResult:
        # Status was already enforced by call_tool; only the callable-adapter presence is checked here.
        adapter = self._adapters.get(entry.name)
        if adapter is None:
            raise ServiceError(
                ErrorCode.TOOL_DISABLED,
                f"Tool {entry.name} is not currently callable (openhands-tools not installed)",
            )
        # Workspace-boundary policy for every filesystem field (independent of lock policy), then
        # pass only the validated canonical paths to the executor.
        arguments = self._validate_action_paths(entry, arguments)
        call_id = _new_call_id()
        started = _now()
        async with self.scheduler.acquire(self._lock_requests(entry, arguments)):
            run = await adapter.run(arguments)
        # Content/structured bounding is applied uniformly by _bound_result in _dispatch.
        return ToolCallResult(
            call_id=call_id,
            tool=entry.name,
            status=CallStatus.COMPLETED if not run.is_error else CallStatus.FAILED,
            is_error=run.is_error,
            content=run.content,
            structured=run.structured,
            metadata={"family": entry.family.value},
            started_at=started,
            finished_at=_now(),
        )

    def _raise_for_status(self, entry: CatalogEntry, status: SupportStatus) -> None:
        if status == SupportStatus.UNSUPPORTED_REQUIRES_AGENT:
            raise ServiceError(ErrorCode.TOOL_REQUIRES_AGENT, f"Tool {entry.name} requires an agent: {entry.reason}")
        if status == SupportStatus.MISSING_DEPENDENCY:
            raise ServiceError(ErrorCode.TOOL_DISABLED, f"Tool {entry.name} is missing a runtime dependency")
        raise ServiceError(ErrorCode.TOOL_DISABLED, f"Tool {entry.name} is not enabled ({status.value})")

    def _bound_result(self, result: ToolCallResult) -> ToolCallResult:
        """Enforce one total response budget across content text, images, and structured data.

        Overflow is spilled to the artifact store (disk) and replaced with a truncation notice or a
        resource link, so a single result cannot hold hundreds of MB in memory or in the response
        body. The artifact store separately caps each artifact's size.
        """
        budget = self.config.max_output_bytes
        used = 0
        bounded: list[ContentBlock] = []
        for block in result.content:
            if block.type == ContentType.TEXT and block.text:
                raw = block.text.encode("utf-8")
                remaining = budget - used
                if remaining <= 0:
                    result.artifacts.append(self.artifacts.save_text(block.text, filename="output.txt"))
                    bounded.append(ContentBlock.of_text("[output omitted; saved as artifact]"))
                    continue
                if len(raw) > remaining:
                    result.artifacts.append(self.artifacts.save_text(block.text, filename="output.txt"))
                    bounded.append(
                        ContentBlock.of_text(
                            raw[:remaining].decode("utf-8", "ignore")
                            + "\n\n[output truncated; full output saved as artifact]"
                        )
                    )
                    used = budget
                    continue
                used += len(raw)
                bounded.append(block)
            elif block.type == ContentType.IMAGE and block.data:
                size = len(block.data)
                if used + size > budget:
                    try:
                        raw = base64.b64decode(block.data)
                    except Exception:
                        raw = block.data.encode("utf-8")
                    descriptor = self.artifacts.save(raw, media_type=block.mime_type or "image/png", filename="image")
                    result.artifacts.append(descriptor)
                    bounded.append(
                        ContentBlock(type=ContentType.RESOURCE_LINK, uri=descriptor.url, mime_type=block.mime_type)
                    )
                else:
                    used += size
                    bounded.append(block)
            else:
                bounded.append(block)
        result.content = bounded
        # Structured payloads can duplicate large content (e.g. full old/new file text); spill the
        # whole payload to an artifact if it exceeds the budget rather than returning it inline.
        if result.structured:
            blob = json.dumps(result.structured, default=str)
            if len(blob.encode("utf-8")) > budget:
                descriptor = self.artifacts.save_text(blob, filename="structured.json")
                result.artifacts.append(descriptor)
                result.structured = {"_truncated": True, "artifact_id": descriptor.artifact_id, "url": descriptor.url}
        return result

    # -- terminal lifecycle facade (policy-checked) -------
    # REST and MCP call these instead of ``self.terminals`` directly so a disabled terminal
    # rejects lifecycle mutations as well as generic/per-tool calls. Internal cleanup (reset/stop)
    # still calls ``self.terminals.reset_all()`` directly — teardown must run regardless of policy.

    async def terminal_create(self, request: TerminalCreateRequest) -> TerminalDescriptor:
        self.ensure_terminal_enabled()
        return await self.terminals.create(request)

    def terminal_list(self) -> list[TerminalDescriptor]:
        self.ensure_terminal_enabled()
        return self.terminals.list()

    def terminal_get(self, terminal_id: str) -> TerminalDescriptor:
        self.ensure_terminal_enabled()
        return self.terminals.get(terminal_id)

    async def terminal_execute(
        self,
        terminal_id: str,
        command: str,
        *,
        timeout: Optional[float] = None,
        is_input: bool = False,
        reset: bool = False,
        call_id: str = "",
    ) -> TerminalResult:
        self.ensure_terminal_enabled()
        return await self.terminals.execute(
            terminal_id, command, timeout=timeout, is_input=is_input, reset=reset, call_id=call_id
        )

    async def terminal_input(
        self, terminal_id: str, text: str, *, timeout: Optional[float] = None, call_id: str = ""
    ) -> TerminalResult:
        self.ensure_terminal_enabled()
        return await self.terminals.input(terminal_id, text, timeout=timeout, call_id=call_id)

    async def terminal_poll(
        self, terminal_id: str, *, timeout: Optional[float] = None, call_id: str = ""
    ) -> TerminalResult:
        self.ensure_terminal_enabled()
        return await self.terminals.poll(terminal_id, timeout=timeout, call_id=call_id)

    async def terminal_interrupt(self, terminal_id: str, *, call_id: str = "") -> TerminalResult:
        self.ensure_terminal_enabled()
        return await self.terminals.interrupt(terminal_id, call_id=call_id)

    async def terminal_reset(self, terminal_id: str) -> TerminalDescriptor:
        self.ensure_terminal_enabled()
        return await self.terminals.reset(terminal_id)

    async def terminal_close(self, terminal_id: str) -> None:
        self.ensure_terminal_enabled()
        await self.terminals.close(terminal_id)

    async def terminal_reset_all(self) -> int:
        self.ensure_terminal_enabled()
        return await self.terminals.reset_all()

    async def terminal_capture(self, terminal_id: str) -> str:
        self.ensure_terminal_enabled()
        return await self.terminals.capture(terminal_id)

    # -- reset --------------------------------------------

    async def reset_environment(self, reason: str = "") -> ResetResponse:
        terminated = await self.terminals.reset_all()
        self._dedup.clear()
        self.artifacts.clear()
        self._environment_generation += 1
        self._environment_id = uuid.uuid4().hex
        if self.config.auto_create_default_terminal and self.terminals.default_backend_ready():
            await self.terminals.ensure_default()
        return ResetResponse(
            reset=True,
            reason=reason,
            environment_generation=self._environment_generation,
            terminated_terminals=terminated,
        )


def _plugin_version() -> str:
    try:
        import idegym.plugins.openhands

        return idegym.plugins.openhands.__version__
    except Exception:
        return "unknown"
