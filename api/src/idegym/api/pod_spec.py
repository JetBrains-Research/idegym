"""Typed, native-Kubernetes-shaped models for customizing the server pod.

Each model mirrors a ``kubernetes(_asyncio).client`` model: fields are declared in snake_case
and (de)serialize to/from camelCase to match Kubernetes manifests (e.g. ``secret_name`` <->
``secretName``). Unmodeled fields are accepted and forwarded verbatim, so the common options
stay strictly typed while the long tail (csi, projected, affinity, ...) remains available.

These compile down to plain camelCase dicts (via ``model_dump(by_alias=True, exclude_none=True)``)
that the orchestrator hands to the Kubernetes client's deserializer.
"""

from typing import Any, Optional

from idegym.api.type import KubernetesNodeSelector
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _KubernetesModel(BaseModel):
    """Base for native-Kubernetes-shaped models: snake_case fields, camelCase (de)serialization,
    and pass-through of any unmodeled fields."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


# --- Volumes -----------------------------------------------------------------


class SecretVolumeSource(_KubernetesModel):
    """Maps to ``V1SecretVolumeSource``."""

    secret_name: Optional[str] = None
    items: Optional[list[dict[str, Any]]] = None
    default_mode: Optional[int] = None
    optional: Optional[bool] = None


class ConfigMapVolumeSource(_KubernetesModel):
    """Maps to ``V1ConfigMapVolumeSource``."""

    name: Optional[str] = None
    items: Optional[list[dict[str, Any]]] = None
    default_mode: Optional[int] = None
    optional: Optional[bool] = None


class EmptyDirVolumeSource(_KubernetesModel):
    """Maps to ``V1EmptyDirVolumeSource``."""

    medium: Optional[str] = None
    size_limit: Optional[str] = None


class PersistentVolumeClaimVolumeSource(_KubernetesModel):
    """Maps to ``V1PersistentVolumeClaimVolumeSource``."""

    claim_name: str
    read_only: Optional[bool] = None


class KubernetesVolume(_KubernetesModel):
    """A pod-level volume (maps to ``V1Volume``); set exactly one source.

    Common sources are typed below; others (``csi``, ``projected``, ``hostPath``,
    ``downwardAPI``, ...) may be passed as extra camelCase keys and are forwarded as-is.
    """

    name: str
    secret: Optional[SecretVolumeSource] = None
    config_map: Optional[ConfigMapVolumeSource] = None
    empty_dir: Optional[EmptyDirVolumeSource] = None
    persistent_volume_claim: Optional[PersistentVolumeClaimVolumeSource] = None


class KubernetesVolumeMount(_KubernetesModel):
    """A container volume mount (maps to ``V1VolumeMount``)."""

    name: str
    mount_path: str
    read_only: Optional[bool] = None
    sub_path: Optional[str] = None
    sub_path_expr: Optional[str] = None
    mount_propagation: Optional[str] = None


# --- envFrom -----------------------------------------------------------------


class SecretEnvSource(_KubernetesModel):
    """Maps to ``V1SecretEnvSource``."""

    name: Optional[str] = None
    optional: Optional[bool] = None


class ConfigMapEnvSource(_KubernetesModel):
    """Maps to ``V1ConfigMapEnvSource``."""

    name: Optional[str] = None
    optional: Optional[bool] = None


class KubernetesEnvFromSource(_KubernetesModel):
    """Bulk environment import for the server container (maps to ``V1EnvFromSource``)."""

    prefix: Optional[str] = None
    secret_ref: Optional[SecretEnvSource] = None
    config_map_ref: Optional[ConfigMapEnvSource] = None


# --- pod_overrides escape hatch ----------------------------------------------


class KubernetesToleration(_KubernetesModel):
    """Maps to ``V1Toleration``."""

    key: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    effect: Optional[str] = None
    toleration_seconds: Optional[int] = None


class KubernetesHostAlias(_KubernetesModel):
    """Maps to ``V1HostAlias``."""

    ip: Optional[str] = None
    hostnames: Optional[list[str]] = None


class KubernetesPodOverrides(_KubernetesModel):
    """Partial pod spec deep-merged into the generated one (maps to ``V1PodSpec``).

    Common pod-level fields are typed below; anything else (``affinity``, ``securityContext``,
    ``dnsConfig``, sidecar ``containers``, ...) may be supplied as extra camelCase keys and is
    forwarded verbatim. Applied last, so it takes precedence over the dedicated request fields on
    key overlap (scalars override, list fields concatenate); the managed server container cannot
    be replaced through it.
    """

    tolerations: Optional[list[KubernetesToleration]] = None
    host_aliases: Optional[list[KubernetesHostAlias]] = None
    node_selector: Optional[KubernetesNodeSelector] = None
    runtime_class_name: Optional[str] = None
    priority_class_name: Optional[str] = None
    scheduler_name: Optional[str] = None
    termination_grace_period_seconds: Optional[int] = None
    automount_service_account_token: Optional[bool] = None
    restart_policy: Optional[str] = None
    dns_policy: Optional[str] = None
