"""Explicit, versioned tool catalog.

The catalog is the single source of truth for which OpenHands tool families are exposed, which are
intentionally unavailable (and why), and how each maps onto a REST route, an MCP tool name, a client
method, and a lock policy. It does NOT rely on ``import openhands.tools`` as the source of truth: the
top-level package curates a subset and excludes browser tools. The audit test compares this manifest
against the installed families and fails on an unclassified addition.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from idegym.plugins.openhands.api.models import Profile, SupportStatus, ToolCapability
from idegym.plugins.openhands.api.names import PUBLIC_PREFIX, ToolFamily, ToolName


class LockScope(StrEnum):
    NONE = "none"  # parallel-safe reads
    TOOL = "tool"  # tool-wide serialization
    PATH = "path"  # per-file lock derived from arguments
    WORKSPACE = "workspace"  # workspace mutation lock
    TERMINAL = "terminal"  # per-terminal lock (handled by the terminal manager)
    BROWSER = "browser"  # per-browser lock


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    family: ToolFamily
    # Classification assuming the family's dependencies are present and the profile allows it.
    base_status: SupportStatus
    profiles: frozenset[Profile] = field(default_factory=frozenset)
    reason: str = ""
    required_extras: tuple[str, ...] = ()
    required_binaries: tuple[str, ...] = ()
    state_scope: str = "stateless"
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    lock_scope: LockScope = LockScope.TOOL
    # True when this canonical tool is dispatched by the terminal manager rather than an adapter.
    is_terminal: bool = False
    # True when this tool takes caller-controlled filesystem path(s). Path/workspace-boundary
    # policy is enforced for these regardless of lock_scope (a read/search tool with LockScope.NONE
    # must still be confined to the workspace).
    filesystem: bool = False

    @property
    def rest_route(self) -> str:
        return f"/api{PUBLIC_PREFIX}/tools/{self.name}"

    @property
    def mcp_name(self) -> str:
        return self.name

    @property
    def client_method(self) -> str:
        return f"tools.{self.name}"


_CORE_FULL = frozenset({Profile.CORE, Profile.FULL})
_FULL_ONLY = frozenset({Profile.FULL})

# The explicit classification manifest. Keep in lockstep with COMPATIBILITY.md.
CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        name=ToolName.TERMINAL,
        family=ToolFamily.TERMINAL,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        state_scope="terminal",
        destructive=True,
        open_world=True,
        lock_scope=LockScope.TERMINAL,
        is_terminal=True,
    ),
    CatalogEntry(
        name=ToolName.FILE_EDITOR,
        family=ToolFamily.FILE_EDITOR,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        state_scope="workspace-file",
        destructive=True,
        lock_scope=LockScope.PATH,
        filesystem=True,
    ),
    CatalogEntry(
        name=ToolName.APPLY_PATCH,
        family=ToolFamily.APPLY_PATCH,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        state_scope="workspace",
        destructive=True,
        lock_scope=LockScope.WORKSPACE,
    ),
    CatalogEntry(
        name=ToolName.GREP,
        family=ToolFamily.GREP,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        read_only=True,
        idempotent=True,
        lock_scope=LockScope.NONE,
        filesystem=True,
    ),
    CatalogEntry(
        name=ToolName.GLOB,
        family=ToolFamily.GLOB,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        read_only=True,
        idempotent=True,
        lock_scope=LockScope.NONE,
        filesystem=True,
    ),
    CatalogEntry(
        name=ToolName.PLANNING_FILE_EDITOR,
        family=ToolFamily.PLANNING_FILE_EDITOR,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        state_scope="plan-file",
        destructive=True,
        lock_scope=LockScope.TOOL,
    ),
    CatalogEntry(
        name=ToolName.TASK_TRACKER,
        family=ToolFamily.TASK_TRACKER,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        state_scope="persistent",
        destructive=True,
        lock_scope=LockScope.TOOL,
    ),
    CatalogEntry(
        name=ToolName.READ_FILE,
        family=ToolFamily.GEMINI,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        read_only=True,
        idempotent=True,
        lock_scope=LockScope.NONE,
        filesystem=True,
    ),
    CatalogEntry(
        name=ToolName.WRITE_FILE,
        family=ToolFamily.GEMINI,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        state_scope="workspace-file",
        destructive=True,
        lock_scope=LockScope.PATH,
        filesystem=True,
    ),
    CatalogEntry(
        name=ToolName.EDIT,
        family=ToolFamily.GEMINI,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        state_scope="workspace-file",
        destructive=True,
        lock_scope=LockScope.PATH,
        filesystem=True,
    ),
    CatalogEntry(
        name=ToolName.LIST_DIRECTORY,
        family=ToolFamily.GEMINI,
        base_status=SupportStatus.ENABLED,
        profiles=_CORE_FULL,
        read_only=True,
        idempotent=True,
        lock_scope=LockScope.NONE,
        filesystem=True,
    ),
    # Browser tools: enabled only in the full profile and only when browser deps are present.
    CatalogEntry(
        name=ToolFamily.BROWSER,
        family=ToolFamily.BROWSER,
        base_status=SupportStatus.ENABLED,
        profiles=_FULL_ONLY,
        state_scope="browser",
        open_world=True,
        destructive=True,
        required_extras=("browser-use",),
        lock_scope=LockScope.BROWSER,
        reason="Browser tools require the full profile and browser runtime dependencies.",
    ),
    # Agent-dependent families: visible as unsupported with a reason, never faked.
    CatalogEntry(
        name=ToolFamily.TASK,
        family=ToolFamily.TASK,
        base_status=SupportStatus.UNSUPPORTED_REQUIRES_AGENT,
        reason="Requires subagent execution and registered agents.",
    ),
    CatalogEntry(
        name=ToolFamily.WORKFLOW,
        family=ToolFamily.WORKFLOW,
        base_status=SupportStatus.UNSUPPORTED_REQUIRES_AGENT,
        reason="Orchestrates subagents and executes dynamic workflow code.",
    ),
    CatalogEntry(
        name=ToolFamily.TOM_CONSULT,
        family=ToolFamily.TOM_CONSULT,
        base_status=SupportStatus.UNSUPPORTED_REQUIRES_AGENT,
        reason="Requires a consultation agent/LLM.",
    ),
    CatalogEntry(
        name=ToolFamily.DELEGATE,
        family=ToolFamily.DELEGATE,
        base_status=SupportStatus.NOT_A_CALLABLE_TOOL,
        reason="Models/visualization support for agent delegation, not a standalone agentless executor.",
    ),
    CatalogEntry(
        name=ToolFamily.PRESET,
        family=ToolFamily.PRESET,
        base_status=SupportStatus.NOT_A_CALLABLE_TOOL,
        reason="Preset/configuration package, not a callable tool.",
    ),
    CatalogEntry(
        name=ToolFamily.UTILS,
        family=ToolFamily.UTILS,
        base_status=SupportStatus.NOT_A_CALLABLE_TOOL,
        reason="Internal support package, not a callable tool.",
    ),
)

# Families that yield callable OpenHands adapters (built from the runtime). Terminal is handled by
# the terminal manager, and browser is deferred, so neither is in this set.
ADAPTER_FAMILIES: tuple[ToolFamily, ...] = (
    ToolFamily.FILE_EDITOR,
    ToolFamily.APPLY_PATCH,
    ToolFamily.GREP,
    ToolFamily.GLOB,
    ToolFamily.PLANNING_FILE_EDITOR,
    ToolFamily.TASK_TRACKER,
    ToolFamily.GEMINI,
)


class ToolCatalog:
    """Applies the active profile + enable/disable overrides to the static manifest."""

    def __init__(self, profile: Profile, enabled_tools: list[str], disabled_tools: list[str]) -> None:
        self._profile = profile
        self._enabled = set(enabled_tools)
        self._disabled = set(disabled_tools)
        self._by_name = {e.name: e for e in CATALOG}

    @property
    def profile(self) -> Profile:
        return self._profile

    def entries(self) -> tuple[CatalogEntry, ...]:
        return CATALOG

    def get(self, name: str) -> CatalogEntry:
        entry = self._by_name.get(name)
        if entry is None:
            raise KeyError(name)
        return entry

    def route_entries(self) -> list[CatalogEntry]:
        """Entries that get a stable per-tool REST/MCP route, from the static manifest.

        Profile-allowed, callable tools (browser is deferred to a later increment). Generated
        without any network call so the route set is stable at import time.
        """
        out = []
        for entry in CATALOG:
            if entry.base_status != SupportStatus.ENABLED:
                continue
            if entry.family == ToolFamily.BROWSER:
                continue
            if self._profile == Profile.CUSTOM:
                if self._enabled and entry.name not in self._enabled:
                    continue
            elif self._profile not in entry.profiles:
                continue
            if entry.name in self._disabled:
                continue
            out.append(entry)
        return out

    def effective_status(
        self, entry: CatalogEntry, *, openhands_available: bool, browser_available: bool
    ) -> SupportStatus:
        """Resolve the runtime status of a catalog entry."""
        if entry.base_status != SupportStatus.ENABLED:
            return entry.base_status
        # custom allow/deny overrides
        if self._profile == Profile.CUSTOM and self._enabled and entry.name not in self._enabled:
            return SupportStatus.DISABLED_BY_PROFILE
        if entry.name in self._disabled:
            return SupportStatus.DISABLED_BY_PROFILE
        # profile gating
        if self._profile in (Profile.CORE, Profile.FULL) and self._profile not in entry.profiles:
            return SupportStatus.DISABLED_BY_PROFILE
        # dependency gating (a missing optional dep must not silently omit the tool)
        if entry.family == ToolFamily.BROWSER and not browser_available:
            return SupportStatus.MISSING_DEPENDENCY
        if not entry.is_terminal and not openhands_available:
            return SupportStatus.MISSING_DEPENDENCY
        return SupportStatus.ENABLED

    def capability(self, entry: CatalogEntry, status: SupportStatus) -> ToolCapability:
        reason = entry.reason
        if status == SupportStatus.MISSING_DEPENDENCY and not reason:
            reason = "openhands-tools is not installed in the service environment."
        return ToolCapability(
            name=entry.name,
            family=entry.family.value,
            status=status,
            reason=reason or None,
            required_extras=list(entry.required_extras),
            required_binaries=list(entry.required_binaries),
            state_scope=entry.state_scope,
            read_only=entry.read_only,
            destructive=entry.destructive,
            idempotent=entry.idempotent,
            open_world=entry.open_world,
            rest_route=entry.rest_route if status == SupportStatus.ENABLED else None,
            mcp_name=entry.mcp_name if status == SupportStatus.ENABLED else None,
            client_method=entry.client_method if status == SupportStatus.ENABLED else None,
        )
