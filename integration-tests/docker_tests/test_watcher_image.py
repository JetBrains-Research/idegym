"""Integration test that builds the watcher Docker image and runs it as a container.

Builds ``watcher/Dockerfile``, pushes it to ``IDEGYM_TEST_REGISTRY`` when set (the CI integration
job runs a local registry; verify locally with a registry on ``localhost:5001``), then runs the
image against a PostgreSQL container and asserts the watcher serves a healthy ``/health`` endpoint.

The watcher never runs migrations and its cleanup loop swallows transient database errors, so
``/health`` comes up regardless of schema state — that is what this test asserts.
"""

import os
import tempfile
import time
from os import environ as env
from unittest import TestCase

import pytest
import requests
from from_root import from_root
from idegym.image.docker_service import DockerService
from python_on_whales import DockerClient, DockerException

pytestmark = pytest.mark.integration

_PG_IMAGE = "postgres:16"
_PG_DB = "idegym"
_PG_USER = "idegym"
_PG_PASSWORD = "idegym"

# The watcher loads Kubernetes configuration at startup. Outside a cluster a syntactically valid
# kubeconfig is enough for the loader to succeed (it parses, it does not connect), which lets the
# /health endpoint come up. The cleanup loop's real Kubernetes calls then fail and are swallowed.
_DUMMY_KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
- name: dummy
  cluster:
    server: https://127.0.0.1:6443
    insecure-skip-tls-verify: true
contexts:
- name: dummy
  context:
    cluster: dummy
    user: dummy
current-context: dummy
users:
- name: dummy
  user:
    token: dummy
"""


class TestWatcherImage(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = env.get("IDEGYM_TEST_REGISTRY", DockerService.REGISTRY)
        cls.client = DockerClient()
        cls.tag = f"{cls.registry}/watcher:test"
        cls.network = None
        cls.postgres = None
        cls.watcher = None

        fd, cls.kubeconfig_path = tempfile.mkstemp(prefix="watcher-it-kubeconfig-")
        with os.fdopen(fd, "w") as handle:
            handle.write(_DUMMY_KUBECONFIG)
        os.chmod(cls.kubeconfig_path, 0o644)

        cls.image = cls.client.build(
            context_path=from_root(),
            file=str(from_root("watcher", "Dockerfile")),
            tags=[cls.tag],
            load=True,
        )
        if cls.registry:
            cls.client.push(cls.tag)

        cls.network = cls.client.network.create("idegym-watcher-it")
        cls.postgres = cls.client.run(
            _PG_IMAGE,
            detach=True,
            name="idegym-watcher-it-pg",
            networks=[cls.network],
            envs={
                "POSTGRES_DB": _PG_DB,
                "POSTGRES_USER": _PG_USER,
                "POSTGRES_PASSWORD": _PG_PASSWORD,
            },
        )
        cls.watcher = cls.client.run(
            cls.tag,
            detach=True,
            name="idegym-watcher-it",
            networks=[cls.network],
            envs={
                "POSTGRES_HOST": "idegym-watcher-it-pg",
                "POSTGRES_PORT": "5432",
                "POSTGRES_DB": _PG_DB,
                "POSTGRES_USER": _PG_USER,
                "POSTGRES_PASSWORD": _PG_PASSWORD,
                "KUBECONFIG": "/home/watcher/.kube/config",
            },
            volumes=[(cls.kubeconfig_path, "/home/watcher/.kube/config", "ro")],
            publish=[(0, 8000)],
        )

    @classmethod
    def tearDownClass(cls):
        for container in (cls.watcher, cls.postgres):
            if container is None:
                continue
            try:
                container.stop(time=1)
                container.remove(volumes=True, force=True)
            except DockerException:
                pass
        if cls.network is not None:
            try:
                cls.network.remove()
            except DockerException:
                pass
        try:
            cls.image.remove(force=True)
        except DockerException:
            pass
        path = getattr(cls, "kubeconfig_path", None)
        if path and os.path.exists(path):
            os.remove(path)

    def _health_url(self) -> str:
        self.watcher.reload()
        binding = self.watcher.network_settings.ports["8000/tcp"][0]
        host_port = binding["HostPort"] if isinstance(binding, dict) else binding.host_port
        return f"http://localhost:{host_port}/health"

    def test_watcher_serves_health(self):
        url = self._health_url()
        deadline = time.time() + 90
        last_error = None
        while time.time() < deadline:
            try:
                response = requests.get(url, timeout=5)
                if response.status_code == 200 and response.json().get("status") == "healthy":
                    return
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            except requests.RequestException as exc:
                last_error = str(exc)
            time.sleep(2)

        logs = ""
        try:
            logs = self.watcher.logs(tail=50)
        except DockerException:
            pass
        self.fail(f"Watcher /health never became healthy. Last error: {last_error}\nLogs:\n{logs}")
