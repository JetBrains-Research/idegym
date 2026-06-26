"""Unit tests for the typed, native-Kubernetes-shaped pod-spec models in idegym.api.pod_spec."""

from idegym.api.pod_spec import (
    KubernetesEnvFromSource,
    KubernetesPodOverrides,
    KubernetesVolume,
    KubernetesVolumeMount,
)


def test_volume_parses_camelcase_and_dumps_back():
    volume = KubernetesVolume.model_validate({"name": "creds", "secret": {"secretName": "agent-creds"}})
    assert volume.secret.secret_name == "agent-creds"  # camelCase -> snake_case attribute
    assert volume.model_dump(by_alias=True, exclude_none=True) == {
        "name": "creds",
        "secret": {"secretName": "agent-creds"},
    }


def test_volume_can_be_built_from_snake_case():
    from idegym.api.pod_spec import SecretVolumeSource

    volume = KubernetesVolume(name="creds", secret=SecretVolumeSource(secret_name="agent-creds"))
    assert volume.model_dump(by_alias=True, exclude_none=True) == {
        "name": "creds",
        "secret": {"secretName": "agent-creds"},
    }


def test_volume_mount_and_env_from_round_trip():
    mount = KubernetesVolumeMount.model_validate({"name": "creds", "mountPath": "/etc/creds", "readOnly": True})
    assert mount.model_dump(by_alias=True, exclude_none=True) == {
        "name": "creds",
        "mountPath": "/etc/creds",
        "readOnly": True,
    }
    env_from = KubernetesEnvFromSource.model_validate({"secretRef": {"name": "agent-creds"}})
    assert env_from.model_dump(by_alias=True, exclude_none=True) == {"secretRef": {"name": "agent-creds"}}


def test_volume_forwards_unmodeled_source_verbatim():
    # csi is not explicitly modeled; it must pass through untouched (extra="allow").
    volume = KubernetesVolume.model_validate({"name": "data", "csi": {"driver": "secrets-store.csi.k8s.io"}})
    assert volume.model_dump(by_alias=True, exclude_none=True) == {
        "name": "data",
        "csi": {"driver": "secrets-store.csi.k8s.io"},
    }


def test_pod_overrides_typed_fields_and_extra_passthrough():
    overrides = KubernetesPodOverrides.model_validate(
        {
            "tolerations": [{"key": "dedicated", "operator": "Exists", "effect": "NoSchedule"}],
            "hostAliases": [{"ip": "10.0.0.1", "hostnames": ["agent.local"]}],
            # unmodeled complex field forwarded verbatim
            "dnsConfig": {"nameservers": ["1.1.1.1"]},
        }
    )
    assert overrides.tolerations[0].key == "dedicated"
    assert overrides.host_aliases[0].ip == "10.0.0.1"
    assert overrides.model_dump(by_alias=True, exclude_none=True) == {
        "tolerations": [{"key": "dedicated", "operator": "Exists", "effect": "NoSchedule"}],
        "hostAliases": [{"ip": "10.0.0.1", "hostnames": ["agent.local"]}],
        "dnsConfig": {"nameservers": ["1.1.1.1"]},
    }


def test_pod_overrides_default_is_empty():
    assert KubernetesPodOverrides().model_dump(by_alias=True, exclude_none=True) == {}
