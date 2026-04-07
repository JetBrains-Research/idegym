from typing import Any, Iterable

import yaml
from idegym.image.plugin import get_plugin_class, get_plugin_type_name


class _ImageDefinitionDumper(yaml.SafeDumper):
    def represent_data(self, data):
        if isinstance(data, str) and "\n" in data:
            return self.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return super().represent_data(data)


def serialize_plugin(plugin: Any) -> dict[str, Any]:
    payload = dict(plugin.to_payload())
    payload.pop("type", None)
    return {"type": get_plugin_type_name(plugin), **payload}


def deserialize_plugin(payload: dict[str, Any]) -> Any:
    type_name = payload.get("type")
    if not isinstance(type_name, str) or not type_name:
        raise ValueError("Plugin payload must contain a non-empty 'type'")
    plugin_class = get_plugin_class(type_name)
    return plugin_class.from_payload({key: value for key, value in payload.items() if key != "type"})


def dump_images(images: Iterable[Any]) -> str:
    return yaml.dump(
        {"images": [image.to_dict() for image in images]},
        sort_keys=False,
        default_flow_style=False,
        Dumper=_ImageDefinitionDumper,
    )


def load_images(value: str | bytes | dict[str, Any], image_class) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)):
        payload = yaml.safe_load(value) or {}
    else:
        payload = value

    if not isinstance(payload, dict):
        raise TypeError(f"Image definition document must be a dict, got {type(payload).__name__}")

    images_payload = payload.get("images", [])
    if not isinstance(images_payload, list):
        raise TypeError(f"Image definition document 'images' must be a list, got {type(images_payload).__name__}")

    return tuple(image_class.model_validate(item) for item in images_payload)
