import json
from typing import Any, Optional

from pydantic_core import PydanticSerializationError


def serialize_as_json_string(data: Any) -> Optional[str]:
    if data is None:
        return None
    try:
        # Pydantic v2 BaseModel duck-typing
        if hasattr(data, "model_dump_json") and callable(getattr(data, "model_dump_json")):
            return data.model_dump_json()
        elif hasattr(data, "model_dump") and callable(getattr(data, "model_dump")):
            return json.dumps(data.model_dump())
        elif isinstance(data, (dict, list)):
            return json.dumps(data)
        elif isinstance(data, str):
            return data
        else:
            return json.dumps(data, default=str)
    except (PydanticSerializationError, TypeError, ValueError):
        # Fallback to string representation if serialization fails
        return str(data)
