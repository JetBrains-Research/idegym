"""Root pytest hooks for suite markers and default selection behavior."""

from pathlib import Path

suffix = "-tests"


def pytest_collection_modifyitems(config, items):
    """
    Mark tests by suite based on their parent directory convention ({suite}-tests).
    """
    deselect_e2e_by_default = not (config.option.markexpr or "").strip()
    selected_items = []
    deselected_items = []

    for item in items:
        pathstr = str(item.fspath)
        for part in Path(pathstr).parts:
            if part.endswith(suffix):
                marker = part.removesuffix(suffix)
                item.add_marker(marker)
                break

        if deselect_e2e_by_default and item.get_closest_marker("e2e"):
            deselected_items.append(item)
            continue

        selected_items.append(item)

    if deselected_items:
        config.hook.pytest_deselected(items=deselected_items)
        items[:] = selected_items
