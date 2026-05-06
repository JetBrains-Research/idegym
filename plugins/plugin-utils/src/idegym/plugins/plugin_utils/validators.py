"""Validation utilities for plugin configuration."""

import re

# Linux username/group: starts with letter or underscore, then letters/digits/hyphens/underscores, max 32 chars.
_LINUX_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def check_linux_id(value: str, field: str) -> str:
    """Validate a Linux username or group name.

    Args:
        value: The identifier to validate.
        field: The field name (used in error messages).

    Returns:
        The validated identifier.

    Raises:
        ValueError: If the identifier does not match the required pattern.
    """
    if not _LINUX_IDENTIFIER_RE.match(value):
        raise ValueError(
            f"Invalid Linux identifier for {field!r}: {value!r}. "
            r"Must match ^[a-z_][a-z0-9_-]{0,31}$"
        )
    return value
