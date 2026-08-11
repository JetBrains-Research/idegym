"""Build the typed configuration from the process environment.

Each leaf field in :mod:`idegym.api.config` generates the environment variable that sets it from
its model's ``env_segment`` and its own name, so the model tree *is* the environment contract and
there is no second list to keep in sync. A field may accept more than one name — a renamed field
keeps reading its old one — so this module walks that tree, takes the first name that is actually
set, and hands the result to ``Config``.
"""

from collections.abc import Collection
from os import environ
from typing import Any, Optional

from idegym.api.config import Config
from idegym.utils.logging import get_logger
from pydantic import BaseModel

logger = get_logger(__name__)

__all__ = [
    "ORCHESTRATOR_SECTIONS",
    "SERVER_SECTIONS",
    "deprecated_variables",
    "environment_aliases",
    "load_config",
]

# Which top-level sections each service reads. These replace the per-service Hydra `defaults:`
ORCHESTRATOR_SECTIONS = frozenset({"logging", "otel", "orchestrator"})
SERVER_SECTIONS = frozenset({"logging", "otel", "project", "server"})


def environment_aliases(model: type[BaseModel] = Config, prefix: str = "") -> dict[str, list[str]]:
    """Map every leaf field's dotted path to the environment variables that set it."""
    aliases: dict[str, list[str]] = {}
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        nested = _nested_model(field.annotation)
        if nested is not None:
            aliases.update(environment_aliases(nested, f"{path}."))
        else:
            aliases[path] = list(field.validation_alias.choices)
    return aliases


def load_config(sections: Collection[str], source: Optional[dict[str, str]] = None) -> Config:
    """Construct ``Config`` from ``source`` (the process environment by default).

    Only fields under one of ``sections`` are read; everything else keeps its default. Where a
    field accepts several names the first one that is set wins, so a deployment carrying both the
    current and the legacy name gets the current one.
    """
    values = environ if source is None else source
    selected = _selected(sections, values)

    data: dict[str, Any] = {}
    for path, variable in selected.items():
        *parents, leaf = path.split(".")
        cursor = data
        for parent in parents:
            cursor = cursor.setdefault(parent, {})
        cursor[leaf] = values[variable]

    for old, current in _deprecated(selected).items():
        logger.warning(f"{old} is deprecated and will be removed; set {current} instead")

    return Config(**data)


def deprecated_variables(sections: Collection[str], source: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Map each legacy variable currently supplying a value to the name that replaces it.

    ``load_config`` logs this itself; the mapping is exposed separately so a health check or an
    upgrade script can ask the same question without building a ``Config``. ``sections`` matches
    :func:`load_config`, so a service only reports the variables it actually reads.
    """
    return _deprecated(_selected(sections, environ if source is None else source))


def _deprecated(selected: dict[str, str]) -> dict[str, str]:
    aliases = environment_aliases()
    return {variable: aliases[path][0] for path, variable in selected.items() if variable != aliases[path][0]}


def _selected(sections: Collection[str], values: Any) -> dict[str, str]:
    """Map each in-scope leaf path to the variable that actually supplies it."""
    if unknown := set(sections) - set(Config.model_fields):
        raise ValueError(f"Unknown configuration sections: {sorted(unknown)}")

    selected: dict[str, str] = {}
    for path, variables in environment_aliases().items():
        section, _, _ = path.partition(".")
        if section not in sections:
            continue
        for variable in variables:
            if variable in values:
                selected[path] = variable
                break
    return selected


def _nested_model(annotation: Any) -> Optional[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None
