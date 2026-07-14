"""Root pytest hooks for suite markers and default selection behavior."""

from pathlib import Path

suffix = "-tests"

# Group markers for the e2e suite, keyed by test-file name prefix, so CI can run balanced parallel
# groups that each recreate the cluster once. A group differs from another only by the pytest ``-m``
# expression. Any e2e test file not listed here falls into the catch-all "other" group
# (``-m "e2e and not idea and not pycharm and not openhands and not kaniko and not mcp"``), so a new
# test file is never silently dropped from CI.
E2E_GROUP_BY_FILE_PREFIX = {
    "test_idea": "idea",
    "test_pycharm": "pycharm",
    "test_openhands": "openhands",
    "test_kaniko_build": "kaniko",
    "test_python_api_build": "kaniko",
    "test_mcp_plugin": "mcp",
    "test_orchestrator_mcp": "mcp",
    "test_server_mcp_proxy": "mcp",
}


def _e2e_group(filename: str) -> str | None:
    for prefix, group in E2E_GROUP_BY_FILE_PREFIX.items():
        if filename.startswith(prefix):
            return group
    return None


def pytest_collection_modifyitems(config, items):
    """
    Mark tests by suite based on their parent directory convention ({suite}-tests).
    """
    deselect_e2e_by_default = not (config.option.markexpr or "").strip()
    selected_items = []
    deselected_items = []

    for item in items:
        pathstr = str(item.fspath)
        is_e2e = False
        for part in Path(pathstr).parts:
            if part.endswith(suffix):
                marker = part.removesuffix(suffix)
                item.add_marker(marker)
                is_e2e = marker == "e2e"
                break

        # Assign a CI group marker to e2e tests based on their file name.
        if is_e2e:
            group = _e2e_group(Path(pathstr).name)
            if group is not None:
                item.add_marker(group)

        if deselect_e2e_by_default and item.get_closest_marker("e2e"):
            deselected_items.append(item)
            continue

        selected_items.append(item)

    if deselected_items:
        config.hook.pytest_deselected(items=deselected_items)
        items[:] = selected_items
