"""Root pytest hooks for suite markers and default selection behavior."""

from pathlib import Path
from typing import Optional


def _suite_marker_for_path(path: Path) -> Optional[str]:
    match path.parts:
        case (*_, "e2e-tests-minikube", _):
            return "e2e"
        case (*_, "integration-tests", _):
            return "integration"
        case (*_, "unit-tests", _):
            return "unit"
        case _:
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
