from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import AfterValidator, AnyHttpUrl, BeforeValidator, IPvAnyAddress, StringConstraints, TypeAdapter


class ConditionStatus(StrEnum):
    """Kubernetes condition `status` values (the `ConditionStatus` type)."""

    TRUE = "True"
    FALSE = "False"
    UNKNOWN = "Unknown"


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


def _check_label_key_prefix_length(key: str) -> str:
    """Cap the DNS-subdomain prefix at 253 characters, which the pattern alone cannot express.

    The name segment is bounded in the pattern, but a dotted prefix cannot be: Pydantic's Rust
    regex engine has no lookahead, and the overall ``max_length`` alone would let a 300-character
    prefix through as long as the name after it is short.
    """
    prefix, slash, _ = key.rpartition("/")
    if slash and len(prefix) > 253:
        raise ValueError(f"the prefix of a label key must be at most 253 characters, got {len(prefix)}")
    return key


# https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/#syntax-and-character-set
# An optional lowercase DNS-subdomain prefix and a slash, then a name segment of at most 63
# characters. Checked here rather than left to the API server, which would otherwise turn a typo
# into a server that fails to start after the request was accepted.
KubernetesLabelKey = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=317,  # 253 (prefix) + 1 (/) + 63 (name)
        # language=regexp
        pattern=(
            r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?"
            r"[a-zA-Z0-9]([a-zA-Z0-9._-]{0,61}[a-zA-Z0-9])?$"
        ),
    ),
    AfterValidator(_check_label_key_prefix_length),
]
KubernetesLabelValue = Annotated[
    str,
    StringConstraints(
        max_length=63,
        # language=regexp
        pattern=r"^([a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?)?$",
    ),
]

# The API server's limit on the combined size of every key and value in an object's annotations.
KUBERNETES_ANNOTATIONS_MAX_TOTAL_BYTES = 256 * 1024


def _check_annotations_total_size(annotations: dict[str, str]) -> dict[str, str]:
    """Hold annotations to the API server's total size limit, counted in bytes as it counts them."""
    total = sum(len(key.encode()) + len(value.encode()) for key, value in annotations.items())
    if total > KUBERNETES_ANNOTATIONS_MAX_TOTAL_BYTES:
        raise ValueError(
            f"annotations may total at most {KUBERNETES_ANNOTATIONS_MAX_TOTAL_BYTES} bytes "
            f"(keys and values), got {total}"
        )
    return annotations


# An annotation key follows the label-key syntax; a value is arbitrary and may be long, as long as
# all of them together stay within the API server's total size limit.
KubernetesAnnotationKey: TypeAlias = KubernetesLabelKey

KubernetesNodeSelector: TypeAlias = dict[KubernetesLabelKey, KubernetesLabelValue]
KubernetesLabels: TypeAlias = dict[KubernetesLabelKey, KubernetesLabelValue]
KubernetesAnnotations: TypeAlias = Annotated[
    dict[KubernetesAnnotationKey, str], AfterValidator(_check_annotations_total_size)
]
AuthType: TypeAlias = Literal["Basic", "Bearer", "Token"]
Duration: TypeAlias = timedelta
LogLevel: TypeAlias = int
LogLevelName: TypeAlias = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
