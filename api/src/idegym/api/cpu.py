from functools import total_ordering
from re import Pattern, compile
from typing import Any, ClassVar, Union

from kubernetes.utils import parse_quantity
from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema


@total_ordering
class CpuQuantity:
    """
    CPU quantity supporting fractional/whole cores and millicore notation.

    Stores values internally as integer millicores.
    Accepts strings like ``"500m"``/``"1.5"`` or
    numeric cores as ``int``/``float``.
    """

    PATTERN: ClassVar[Pattern] = compile(r"^(\d*\.?\d+)(m)?$")

    def __init__(self, *, cores: int | float = 0, millicores: int = 0):
        total = round(cores * 1000) + millicores
        if total < 0:
            raise ValueError("CpuQuantity cannot be negative")
        self._millicores = total

    @classmethod
    def parse(cls, value: str) -> "CpuQuantity":
        try:
            normalized = value.strip()
            matcher = cls.PATTERN.match(normalized)
            if not matcher:
                raise ValueError(f"'{value}' does not match CPU quantity pattern")
            amount, suffix = matcher.groups()
            if suffix == "m" and "." in amount:
                raise ValueError(f"Millicore values must be integers: {amount}")
            cores = parse_quantity(normalized)
            if cores < 0:
                raise ValueError("CpuQuantity cannot be negative")
            return cls(millicores=int(cores * 1000))
        except ValueError:
            raise
        except Exception as ex:
            raise ValueError(f"'{value}' is not a valid CPU quantity") from ex

    @property
    def millicores(self) -> int:
        return self._millicores

    @property
    def cores(self) -> float:
        return self._millicores / 1000

    def __eq__(self, other: object) -> bool:
        if self is other:
            return True
        match other:
            case CpuQuantity():
                return self._millicores == other._millicores
            case int() | float():
                return self._millicores == round(other * 1000)
            case str():
                return self._millicores == CpuQuantity.parse(other)._millicores
            case _:
                return False

    def __lt__(self, other: Union["CpuQuantity", int, float, str]) -> bool:
        match other:
            case CpuQuantity():
                return self._millicores < other._millicores
            case int() | float():
                return self._millicores < round(other * 1000)
            case str():
                return self._millicores < CpuQuantity.parse(other)._millicores
            case _:
                return NotImplemented

    def __str__(self) -> str:
        if self._millicores % 1000 == 0:
            return str(self._millicores // 1000)
        return f"{self._millicores}m"

    def __repr__(self) -> str:
        return f"CpuQuantity(millicores={self._millicores})"

    def __hash__(self) -> int:
        return hash(self._millicores)

    @classmethod
    def validate(cls, value: Union["CpuQuantity", int, float, str]) -> "CpuQuantity":
        match value:
            case cls():
                return value
            case int() | float():
                return cls(cores=value)
            case str():
                return cls.parse(value)
            case _:
                raise ValueError(f"Cannot convert {value} to {cls.__name__}")

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        _source_type: Any,
        _handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        return core_schema.union_schema(
            [
                core_schema.is_instance_schema(cls),
                core_schema.chain_schema(
                    [
                        core_schema.int_schema(),
                        core_schema.no_info_plain_validator_function(cls.validate),
                    ]
                ),
                core_schema.chain_schema(
                    [
                        core_schema.float_schema(),
                        core_schema.no_info_plain_validator_function(cls.validate),
                    ]
                ),
                core_schema.chain_schema(
                    [
                        core_schema.str_schema(),
                        core_schema.no_info_plain_validator_function(cls.parse),
                    ]
                ),
            ],
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        _core_schema: core_schema.CoreSchema,
        _handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        return {
            "type": "string",
            "description": "CPU quantity in cores or millicores (e.g. '500m', '1', '2.5')",
            "examples": ["100m", "250m", "500m", "1", "1500m", "2"],
        }
