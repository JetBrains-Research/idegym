from datetime import timedelta
from typing import Annotated, Dict, Literal, Type, TypeAlias

from pydantic import AnyHttpUrl, BeforeValidator, IPvAnyAddress, StringConstraints, TypeAdapter

ipv_address_adapter = TypeAdapter(IPvAnyAddress)
http_url_adapter = TypeAdapter(AnyHttpUrl)

HttpUrl = Annotated[str, BeforeValidator(lambda value: str(http_url_adapter.validate_python(value)))]
IPvAddress = Annotated[str, BeforeValidator(lambda value: str(ipv_address_adapter.validate_python(value)))]

# https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#rfc-1035-label-names
KubernetesObjectName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=63,
        # language=regexp
        pattern="^[a-z]([-a-z0-9]*[a-z0-9])?$",
    ),
]
# https://github.com/opencontainers/distribution-spec/blob/main/spec.md#workflow-categories
OCIImageName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=383,  # The maximum length is set to 255 + 128
        # language=regexp
        pattern="^[a-z0-9._/:@-]+$",
    ),
]
# https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
KubernetesLabelKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=317,  # 253 (prefix) + 1 (/) + 63 (name)
        # language=regexp
        pattern=r"^([a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?/)?[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$",
    ),
]
KubernetesLabelValue = Annotated[
    str,
    StringConstraints(
        max_length=63,
        # language=regexp
        pattern=r"^([a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?)?$",
    ),
]


KubernetesNodeSelector: TypeAlias = Dict[KubernetesLabelKey, KubernetesLabelValue]
AuthType: Type[str] = Literal["Basic", "Bearer", "Token"]
Duration: TypeAlias = timedelta
LogLevel: TypeAlias = int
LogLevelName: Type[str] = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
