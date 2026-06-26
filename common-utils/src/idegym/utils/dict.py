from collections.abc import Generator
from typing import Any


def walk(dictionary: dict[Any, Any]) -> Generator[Any, None, None]:
    """Yield all leaf values from a nested dictionary, depth-first."""
    for value in dictionary.values():
        yield from (walk(value) if isinstance(value, dict) else [value])


def deep_merge(base: dict[Any, Any], override: dict[Any, Any], *, concat_lists: bool = False) -> dict[Any, Any]:
    """Recursively merge ``override`` into a copy of ``base``.

    - dict + dict: merged recursively
    - list + list: concatenated (base first) when ``concat_lists`` else replaced by ``override``
    - otherwise: the ``override`` value wins

    ``base`` is not mutated.
    """
    merged = dict(base)
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, dict) and isinstance(override_value, dict):
            merged[key] = deep_merge(base_value, override_value, concat_lists=concat_lists)
        elif concat_lists and isinstance(base_value, list) and isinstance(override_value, list):
            merged[key] = base_value + override_value
        else:
            merged[key] = override_value
    return merged
