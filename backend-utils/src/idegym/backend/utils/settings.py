"""Build the typed configuration from the process environment.

Each leaf field in :mod:`idegym.api.config` declares the environment variable that sets it as its
``validation_alias``, so the model tree *is* the environment contract and there is no second list
to keep in sync. This module walks that tree, collects the variables that are actually set, and
hands the result to ``Config``.
"""

from collections.abc import Collection
from os import environ
from typing import Any, Optional

from idegym.api.config import Config
from pydantic import BaseModel

__all__ = ["ORCHESTRATOR_SECTIONS", "SERVER_SECTIONS", "environment_aliases", "load_config"]

# Which top-level sections each service reads. These replace the per-service Hydra `defaults:`
ORCHESTRATOR_SECTIONS = frozenset({"logging", "otel", "orchestrator"})
SERVER_SECTIONS = frozenset({"logging", "otel", "project", "server"})


def environment_aliases(model: type[BaseModel] = Config, prefix: str = "") -> dict[str, str]:
    """Map every leaf field's dotted path to the environment variable that sets it."""
    aliases: dict[str, str] = {}
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        nested = _nested_model(field.annotation)
        if nested is not None:
            aliases.update(environment_aliases(nested, f"{path}."))
        elif isinstance(field.validation_alias, str):
            aliases[path] = field.validation_alias
    return aliases


def load_config(sections: Collection[str], source: Optional[dict[str, str]] = None) -> Config:
    """Construct ``Config`` from ``source`` (the process environment by default).

    Only fields under one of ``sections`` are read; everything else keeps its default.
    """
    if unknown := set(sections) - set(Config.model_fields):
        raise ValueError(f"Unknown configuration sections: {sorted(unknown)}")

    values = environ if source is None else source
    data: dict[str, Any] = {}
    for path, variable in environment_aliases().items():
        section, _, _ = path.partition(".")
        if section not in sections or variable not in values:
            continue
        *parents, leaf = path.split(".")
        cursor = data
        for parent in parents:
            cursor = cursor.setdefault(parent, {})
        cursor[leaf] = values[variable]

    return Config(**data)


def _nested_model(annotation: Any) -> Optional[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None
