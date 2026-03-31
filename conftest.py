"""Root pytest hooks for suite markers and default selection behavior."""

from pathlib import Path


def _suite_marker_for_path(path: Path) -> str | None:
    parts = path.parts

    if "unit-tests" in parts:
        return "unit"
    if "integration-tests" in parts:
        return "integration"
    if "e2e-tests-minikube" in parts:
        e2e_dir_idx = parts.index("e2e-tests-minikube")
        if e2e_dir_idx + 1 < len(parts) and parts[e2e_dir_idx + 1] == "tests":
            return "e2e"

    return None


def pytest_collection_modifyitems(config, items):
    """Mark tests by suite and keep local default runs fast by deselecting e2e."""
    deselect_e2e_by_default = not (config.option.markexpr or "").strip()
    selected_items = []
    deselected_items = []

    for item in items:
        marker = _suite_marker_for_path(Path(str(item.fspath)))
        if marker:
            item.add_marker(marker)

        if deselect_e2e_by_default and marker == "e2e":
            deselected_items.append(item)
            continue

        selected_items.append(item)

    if deselected_items:
        config.hook.pytest_deselected(items=deselected_items)
        items[:] = selected_items
