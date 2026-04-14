from collections.abc import Generator
from typing import Any


def walk(dictionary: dict[Any, Any]) -> Generator[Any, None, None]:
    """Yield all leaf values from a nested dictionary, depth-first."""
    for value in dictionary.values():
        yield from (walk(value) if isinstance(value, dict) else [value])
