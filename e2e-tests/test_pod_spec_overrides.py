"""End-to-end tests for pod-spec customization on StartServerRequest (JBRes-9717):
mounting secrets as volumes, importing them via envFrom, setting a ServiceAccount, and the
generic pod_overrides escape hatch.

These require a live Kubernetes cluster (gVisor + the idegym release) and are part of the
e2e suite, which is deselected by default. Run explicitly with `uv run pytest -m e2e`.
"""

import pytest
from idegym.api.pod_spec import (
    KubernetesEnvFromSource,
    KubernetesHostAlias,
    KubernetesPodOverrides,
    KubernetesVolume,
    KubernetesVolumeMount,
    SecretEnvSource,
    SecretVolumeSource,
)
from utils import k8s_client
from utils.constants import DEFAULT_NAMESPACE, DEFAULT_SERVER_START_TIMEOUT
from utils.idegym_utils import create_http_client


@pytest.mark.asyncio
async def test_secret_volume_mount_and_env_from(test_image, test_id):
    """A secret can be mounted as a volume and imported into the container env via env_from."""
    secret_name = f"e2e-secret-{test_id}"
    string_data = {"SECRET_TOKEN": "s3cr3t-token-value", "API_BASE": "https://api.example.test"}
    k8s_client.create_secret(namespace=DEFAULT_NAMESPACE, name=secret_name, string_data=string_data)
    try:
        async with create_http_client(name=f"podspec-{test_id}", nodes_count=0) as client:
            async with client.with_server(
                image_tag=test_image,
                server_name=f"secret-{test_id}",
                runtime_class_name="gvisor",
                server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
                volumes=[KubernetesVolume(name="agent-secret", secret=SecretVolumeSource(secret_name=secret_name))],
                volume_mounts=[
                    KubernetesVolumeMount(name="agent-secret", mount_path="/etc/agent-secret", read_only=True)
                ],
                env_from=[KubernetesEnvFromSource(secret_ref=SecretEnvSource(name=secret_name))],
            ) as server:
                # Mounted as files, one per secret key.
                mounted = await server.execute_bash(script="cat /etc/agent-secret/SECRET_TOKEN")
                assert mounted.exit_code == 0, mounted.stderr
                assert mounted.stdout.strip() == "s3cr3t-token-value"

                # Imported as environment variables.
                env = await server.execute_bash(script='printf "%s" "$API_BASE"')
                assert env.exit_code == 0, env.stderr
                assert env.stdout.strip() == "https://api.example.test"
    finally:
        k8s_client.delete_secret(namespace=DEFAULT_NAMESPACE, name=secret_name)


@pytest.mark.asyncio
async def test_service_account_name(test_image, test_id):
    """A caller-supplied service_account_name is applied to the server pod."""
    sa_name = f"e2e-sa-{test_id}"
    k8s_client.create_service_account(namespace=DEFAULT_NAMESPACE, name=sa_name)
    try:
        async with create_http_client(name=f"podspec-{test_id}", nodes_count=0) as client:
            async with client.with_server(
                image_tag=test_image,
                server_name=f"sa-{test_id}",
                runtime_class_name="gvisor",
                server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
                service_account_name=sa_name,
            ) as server:
                # Server is up...
                ready = await server.execute_bash(script="true")
                assert ready.exit_code == 0, ready.stderr

                # ...and its pod runs under the requested ServiceAccount.
                pods = k8s_client.list_pods(
                    namespace=DEFAULT_NAMESPACE, label_selector="app.kubernetes.io/component=sandbox"
                )
                assert any(pod.spec.service_account_name == sa_name for pod in pods), (
                    f"no sandbox pod runs under ServiceAccount {sa_name}"
                )
    finally:
        k8s_client.delete_service_account(namespace=DEFAULT_NAMESPACE, name=sa_name)


@pytest.mark.asyncio
async def test_pod_overrides_host_aliases(test_image, test_id):
    """pod_overrides applies arbitrary pod-level fields (here hostAliases -> /etc/hosts)."""
    async with create_http_client(name=f"podspec-{test_id}", nodes_count=0) as client:
        async with client.with_server(
            image_tag=test_image,
            server_name=f"override-{test_id}",
            runtime_class_name="gvisor",
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            pod_overrides=KubernetesPodOverrides(
                host_aliases=[KubernetesHostAlias(ip="10.123.45.67", hostnames=["agent.internal.test"])]
            ),
        ) as server:
            result = await server.execute_bash(script="cat /etc/hosts")
            assert result.exit_code == 0, result.stderr
            assert "10.123.45.67" in result.stdout
            assert "agent.internal.test" in result.stdout
