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


def serialize_image(image: Any) -> dict[str, Any]:
    payload = {"base": image.base}
    if image.name is not None:
        payload["name"] = image.name
    if image._plugins:
        payload["plugins"] = [serialize_plugin(plugin) for plugin in image._plugins]
    if image._commands:
        payload["commands"] = list(image._commands)
    if image._platforms:
        payload["platforms"] = list(image._platforms)
    if image._runtime_class_name != "gvisor":
        payload["runtime_class_name"] = image._runtime_class_name
    if image._resources is not None:
        payload["resources"] = dict(image._resources)
    return payload


def deserialize_image(payload: dict[str, Any], image_class: type) -> Any:
    base = payload.get("base")
    if not isinstance(base, str) or not base:
        raise ValueError("Image payload must contain a non-empty 'base'")

    name = payload.get("name")
    if name is not None and not isinstance(name, str):
        raise TypeError(f"Image 'name' must be a string, got {type(name).__name__}")

    plugins_payload = payload.get("plugins", [])
    if not isinstance(plugins_payload, list):
        raise TypeError(f"Image 'plugins' must be a list, got {type(plugins_payload).__name__}")
    plugins: list[Any] = []
    for item in plugins_payload:
        if not isinstance(item, dict):
            raise TypeError(f"Image plugins must be dicts, got {type(item).__name__}")
        plugins.append(deserialize_plugin(item))

    commands_payload = payload.get("commands", [])
    if not isinstance(commands_payload, list):
        raise TypeError(f"Image 'commands' must be a list, got {type(commands_payload).__name__}")
    if not all(isinstance(command, str) for command in commands_payload):
        raise TypeError("Image 'commands' must contain only strings")

    platforms_payload = payload.get("platforms", [])
    if not isinstance(platforms_payload, list):
        raise TypeError(f"Image 'platforms' must be a list, got {type(platforms_payload).__name__}")
    if not all(isinstance(platform, str) for platform in platforms_payload):
        raise TypeError("Image 'platforms' must contain only strings")

    runtime_class_name = payload.get("runtime_class_name", "gvisor")
    if not isinstance(runtime_class_name, str) or not runtime_class_name:
        raise TypeError(
            f"Image 'runtime_class_name' must be a non-empty string, got {type(runtime_class_name).__name__}"
        )

    resources = payload.get("resources")
    if resources is not None and not isinstance(resources, dict):
        raise TypeError(f"Image 'resources' must be a dict, got {type(resources).__name__}")

    return image_class(
        base=base,
        name=name,
        _plugins=tuple(plugins),
        _commands=tuple(commands_payload),
        _platforms=tuple(platforms_payload),
        _runtime_class_name=runtime_class_name,
        _resources=dict(resources) if resources is not None else None,
    )


def dump_images(images: Iterable[Any]) -> str:
    return yaml.dump(
        {"images": [serialize_image(image) for image in images]},
        sort_keys=False,
        default_flow_style=False,
        Dumper=_ImageDefinitionDumper,
    )


def load_images(value: str | bytes | dict[str, Any], image_class: type) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)):
        payload = yaml.safe_load(value) or {}
    else:
        payload = value

    if not isinstance(payload, dict):
        raise TypeError(f"Image definition document must be a dict, got {type(payload).__name__}")

    images_payload = payload.get("images", [])
    if not isinstance(images_payload, list):
        raise TypeError(f"Image definition document 'images' must be a list, got {type(images_payload).__name__}")

    images: list[Any] = []
    for item in images_payload:
        if not isinstance(item, dict):
            raise TypeError(f"Image definitions must be dicts, got {type(item).__name__}")
        images.append(deserialize_image(item, image_class))
    return tuple(images)
