from typing import Any, Dict, Generator


def walk(dictionary: Dict[Any, Any]) -> Generator[Any, None, None]:
    for value in dictionary.values():
        yield from (walk(value) if isinstance(value, dict) else [value])
