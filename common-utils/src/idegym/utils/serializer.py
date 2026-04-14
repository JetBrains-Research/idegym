import json
from typing import Any, Optional

from pydantic_core import PydanticSerializationError


def serialize_as_json_string(data: Any) -> Optional[str]:
    """Serialize an arbitrary value to a JSON string.

    Handles Pydantic v2 models via duck-typing (``model_dump_json`` preferred
    over ``model_dump`` for efficiency), plain dicts/lists, bare strings, and
    falls back to ``json.dumps(..., default=str)`` for other types.
    Returns ``None`` if ``data`` is ``None``. On serialization failure, falls
    back to ``str(data)``.
    """
    if data is None:
        return None
    try:
        if hasattr(data, "model_dump_json") and callable(data.model_dump_json):
            return data.model_dump_json()
        elif hasattr(data, "model_dump") and callable(data.model_dump):
            return json.dumps(data.model_dump())
        elif isinstance(data, (dict, list)):
            return json.dumps(data)
        elif isinstance(data, str):
            return data
        else:
            return json.dumps(data, default=str)
    except (PydanticSerializationError, TypeError, ValueError):
        return str(data)
