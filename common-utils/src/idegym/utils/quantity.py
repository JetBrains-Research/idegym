import re
from decimal import Decimal


def parse_quantity(quantity_str: str) -> int | float:
    """Parse a Kubernetes quantity string into a plain numeric value.

    Supports binary suffixes (Ki, Mi, Gi, Ti, Pi, Ei) returning ``int``,
    decimal suffixes (m, k, M, G, T, P, E) returning ``float``,
    and bare numeric strings returning ``int`` or ``float`` depending on
    whether the value is whole.

    Raises ``ValueError`` for unrecognised formats or unknown suffixes.
    """
    if not quantity_str:
        return 0

    binary_suffixes = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Pi": 1024**5,
        "Ei": 1024**6,
    }

    decimal_suffixes = {
        "m": Decimal("0.001"),
        "k": Decimal("1000"),
        "M": Decimal("1000000"),
        "G": Decimal("1000000000"),
        "T": Decimal("1000000000000"),
        "P": Decimal("1000000000000000"),
        "E": Decimal("1000000000000000000"),
    }

    match = re.match(r"^(\d*\.?\d+)([a-zA-Z]*)$", quantity_str.strip())
    if not match:
        raise ValueError(f"Invalid quantity format: {quantity_str}")

    value_str, suffix = match.groups()
    value = Decimal(value_str)

    if suffix in binary_suffixes:
        return int(value * binary_suffixes[suffix])
    elif suffix in decimal_suffixes:
        return float(value * decimal_suffixes[suffix])
    elif not suffix:
        return int(value) if value == int(value) else float(value)
    else:
        raise ValueError(f"Unknown suffix: {suffix}")
