"""Build-time IDE settings files, defined once and shared by the idea/pycharm plugins.

The JetBrains IDEs read a handful of XML/settings files from the config dir to pre-accept the
data-sharing consent, disable cloud plugins, trust the project, suppress first-run dialogs, and
auto-start the MCP server. Their content lives here as data — edited as ordinary multi-line XML —
and is rendered into ``RUN`` instructions by :func:`render_*_config`, shared by both plugins so the
settings never drift apart.

Every XML document is parsed by :func:`_xml` at import time, so a malformed edit fails fast instead
of silently shipping broken XML.
"""

from xml.etree import ElementTree

# ── Settings file contents (edited as plain text/XML; validated below) ─────────────────

# Pre-accept the JetBrains data-sharing consent so no modal blocks a headful startup.
_CONSENT_ACCEPTED = ["rsch.send.usage.stat:1.1:1:1700000000000"]

# Cloud/marketplace plugins that pop blocking modals (and the OS-integration daemon, which refuses to
# run as root and takes the whole IDE down) — disabled for an automated container.
_DISABLED_PLUGINS = [
    "com.intellij.marketplace",
    "com.intellij.settingsSync",
    "com.jetbrains.codeWithMe",
    "com.intellij.platform.daemon",
]


def _xml(doc: str) -> list[str]:
    """Validate ``doc`` is well-formed XML and return it split into lines for ``printf``."""
    ElementTree.fromstring(doc)  # raises ParseError on malformed XML — fail the build early
    return doc.split("\n")


_UPDATES_XML = _xml(
    """<application>
  <component name="UpdatesConfigurable">
    <option name="SHOW_WHATS_NEW_EDITOR" value="false" />
  </component>
</application>"""
)

_WHATS_NEW_XML = _xml(
    """<application>
  <component name="PropertyService"><![CDATA[{
  "keyToString": {
    "whats.new.last.shown.version": "9999-9.9-99999"
  }
}]]></component>
</application>"""
)

_MCP_SERVER_XML = _xml(
    """<application>
  <component name="McpServerSettings">
    <option name="enableMcpServer" value="true" />
    <option name="enableBraveMode" value="true" />
  </component>
</application>"""
)

_IDE_GENERAL_XML = _xml(
    """<application>
  <component name="GeneralSettings">
    <option name="showTipsOnStartup" value="false" />
  </component>
</application>"""
)

_PRIVACY_POLICY_XML = _xml(
    """<application>
  <component name="PrivacyPolicyAgreement">
    <option name="versionOfAccepted" value="999.999"/>
  </component>
</application>"""
)

# PyCharm-only: mark the tool-window stripe buttons as already added so the first-run tour is skipped.
_PROPERTIES_XML = _xml(
    """<application>
  <component name="PropertiesComponent">
    <property name="toolwindow.stripes.buttons.added" value="true" />
  </component>
</application>"""
)


def _trusted_paths_xml(project_root: str) -> list[str]:
    return _xml(
        f"""<application>
  <component name="Trusted.Paths.Settings">
    <option name="TRUSTED_PATHS">
      <list>
        <option value="{project_root}" />
      </list>
    </option>
    <option name="TRUSTED_PROJECT_LOCATORS">
      <list />
    </option>
  </component>
</application>"""
    )


# ── Rendering settings files into Dockerfile RUN instructions ──────────────────────────


def _squote(line: str) -> str:
    if "'" in line:  # single-quoting is safe only because no settings line contains a single quote
        raise ValueError(f"config line may not contain a single quote: {line!r}")
    return f"'{line}'"


def _write_stmt(dest: str, lines: list[str]) -> list[str]:
    """Physical lines of a ``printf '%s\\n' 'line' ... > dest`` statement (no continuations yet)."""
    return ["printf '%s\\n'", *(f"    {_squote(line)}" for line in lines), f"    > {dest}"]


def _run(*statements: list[str]) -> str:
    """Assemble shell ``statements`` (each a list of physical lines) into one ``RUN`` instruction.

    Statements are separated by ``;``; every physical line but the last gets a ``\\`` continuation.
    """
    physical: list[str] = []
    for stmt_index, stmt in enumerate(statements):
        for line_index, line in enumerate(stmt):
            last_of_stmt = line_index == len(stmt) - 1
            final_stmt = stmt_index == len(statements) - 1
            physical.append(line + (";" if last_of_stmt and not final_stmt else ""))
    out = []
    for i, line in enumerate(physical):
        prefix = "RUN " if i == 0 else "    "
        suffix = " \\" if i < len(physical) - 1 else ""
        out.append(f"{prefix}{line}{suffix}")
    return "\n".join(out)


def _with_comment(comment: str, run: str) -> str:
    return f"{comment}\n{run}"


def render_entrypoint_install() -> str:
    """Install the shared entrypoint: check-mcp, the start-ide launcher, and the supervisord program.

    All three are IDE-agnostic (the IDE-specific config is baked in as ENV) and identical across the
    idea/pycharm plugins, so they ship once in plugin-utils and install under a single command and
    supervisord program. Emitted for both the open-project and mcp-steroid entrypoints.
    """
    return (
        "# The check-mcp probe and start-ide entrypoint are IDE-agnostic (IDE-specific config is baked\n"
        "# in as ENV) and shared by the idea/pycharm plugins, under a single command + supervisord\n"
        "# program. Installed without the .sh suffix so the names survive IdeGYMServer's\n"
        "# /usr/local/bin/*.{py,sh} -> bare-command rename pass (which would otherwise break the\n"
        "# supervisord command and start-ide's internal source of check-mcp).\n"
        "COPY plugins/plugin-utils/scripts/check-mcp.sh /usr/local/bin/check-mcp\n"
        "COPY plugins/plugin-utils/scripts/start-ide.sh /usr/local/bin/start-ide\n"
        "RUN chmod +x /usr/local/bin/check-mcp /usr/local/bin/start-ide\n"
        "COPY plugins/plugin-utils/scripts/supervisord-ide.conf /etc/supervisor/conf.d/ide.conf"
    )


def render_first_run_config(config_dir: str) -> str:
    """Consent + disabled-plugins + update/What's-New suppression written at install time.

    Emitted by PyCharm always and by IDEA only for virtual-display builds (headless IDEA starts none
    of the native components these settings tame).
    """
    comment = (
        "# Pre-accept the data-sharing consent, disable the cloud/marketplace plugins and the\n"
        "# OS-integration daemon (which refuses to run as root and takes the IDE down), and suppress\n"
        "# the What's New editor so nothing blocks startup."
    )
    run = _run(
        ["set -eux"],
        [f'mkdir -p "{config_dir}/options"'],
        ["mkdir -p /root/.local/share/JetBrains/consentOptions"],
        _write_stmt("/root/.local/share/JetBrains/consentOptions/accepted", _CONSENT_ACCEPTED),
        _write_stmt(f'"{config_dir}/disabled_plugins.txt"', _DISABLED_PLUGINS),
        _write_stmt(f'"{config_dir}/options/updates.xml"', _UPDATES_XML),
        _write_stmt(f'"{config_dir}/options/other.xml"', _WHATS_NEW_XML),
    )
    return _with_comment(comment, run)


def render_mcp_config(config_dir: str, mcp_port: int, bridge_port: int) -> str:
    """Enable MCP auto-start (``mcpServer.xml``) at the IDE config path."""
    comment = (
        "# Enable the JetBrains MCP server plugin (bundled in 2026.1.1+).\n"
        "# Versions / manual download: https://plugins.jetbrains.com/plugin/26071-mcp-server/versions\n"
        f"# Binds to 127.0.0.1:{mcp_port}; the start script bridges it to 0.0.0.0:{bridge_port}."
    )
    run = _run(
        ["set -eux"],
        [f'mkdir -p "{config_dir}/options"'],
        _write_stmt(f'"{config_dir}/options/mcpServer.xml"', _MCP_SERVER_XML),
    )
    return _with_comment(comment, run)


def render_open_project_config(config_dir: str, project_root: str, *, privacy: bool, properties: bool) -> str:
    """First-run/trust settings for the open-project workflow.

    ``privacy`` adds ``privacyPolicy.xml`` (IDEA; PyCharm accepts the EUA via vmoptions instead).
    ``properties`` adds the PyCharm-only ``PropertiesComponent`` ``other.xml``.
    """
    files: list[tuple[str, list[str]]] = []
    if properties:
        files.append((f'"{config_dir}/options/other.xml"', _PROPERTIES_XML))
    files.append((f'"{config_dir}/options/ide.general.xml"', _IDE_GENERAL_XML))
    files.append((f'"{config_dir}/options/trusted-paths.xml"', _trusted_paths_xml(project_root)))
    if privacy:
        files.append((f'"{config_dir}/options/privacyPolicy.xml"', _PRIVACY_POLICY_XML))
    run = _run(
        ["set -eux"],
        [f'mkdir -p "{config_dir}/options"'],
        *(_write_stmt(dest, lines) for dest, lines in files),
    )
    return _with_comment("# Trust the project path and suppress first-run wizard / EUA dialogs.", run)
