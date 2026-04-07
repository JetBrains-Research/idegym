from importlib.resources import files
from os import environ as env
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest import TestCase, main

from idegym.api.download import DownloadRequest
from idegym.api.git import GitRepository, GitServer
from idegym.image.docker_service import __CONTAINER_VOLUME_PATH__, DockerService
from idegym.utils.path import get_base_filename
from python_on_whales import DockerClient, DockerException, Image


class TestDockerService(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = env.get("IDEGYM_TEST_REGISTRY", DockerService.REGISTRY)
        cls.client = DockerClient()
        cls.containers = []
        cls.images = []
        cls.scripts = []

        for idx, content in enumerate([b"#!/bin/sh\necho 'Script 1'\n", b"#!/bin/sh\necho 'Script 2'\n"]):
            temporary = NamedTemporaryFile(
                prefix=f"test_script_{idx}_",
                suffix=".sh",
                delete=False,
            )
            temporary.write(content)
            temporary.flush()
            cls.scripts.append(temporary)

        cls.repository = GitRepository(
            server=GitServer.GITHUB,
            owner="spring-projects",
            name="spring-petclinic",
        )

        cls.snapshot = cls.repository.head()
        cls.request = DownloadRequest(descriptor=cls.snapshot.descriptor())

        cls.service = DockerService(cls.client)
        dockerfile = files().joinpath("Dockerfile.test")
        tag = f"{cls.registry}/server-alpine-latest:test"
        image = cls.client.build(
            context_path=Path.cwd(),
            file=str(dockerfile),
            tags=[tag],
            load=True,
        )
        if cls.registry:
            cls.client.push(tag)
        cls.images.append(image)

    @classmethod
    def tearDownClass(cls):
        for script in cls.scripts:
            try:
                script.close()
            except OSError:
                pass

        for container in cls.containers:
            try:
                container.stop(time=1)
                container.remove(volumes=True, force=True)
            except DockerException:
                pass

        for image in cls.images:
            try:
                image.remove(force=True)
            except DockerException:
                pass

    def assertImageCorrect(self, image: Image, request: DownloadRequest, image_version: str):
        self.assertIsNotNone(image)
        self.assertTrue(image.id)
        image_name = get_base_filename(request.descriptor.name)
        expected_tag = f"{DockerService.REGISTRY}/{image_name}:{image_version}"
        self.assertIn(expected_tag, image.repo_tags)

    def test_build_image(self):
        image = self.service.build(
            request=self.request,
            image_base="alpine-latest",
            image_version=self.test_build_image.__name__,
            service_version="test",
            registry=self.registry,
        )
        self.images.append(image)
        self.assertImageCorrect(image, self.request, self.test_build_image.__name__)

    def test_build_image_with_additional_lines(self):
        key = "idegym"
        value = "Hello, IdeGYM!"
        labels = [f"LABEL {key}='{value}'"]
        image = self.service.build(
            request=self.request,
            image_base="alpine-latest",
            image_version=self.test_build_image_with_additional_lines.__name__,
            service_version="test",
            commands=labels,
            registry=self.registry,
        )
        self.assertImageCorrect(image, self.request, self.test_build_image_with_additional_lines.__name__)
        self.assertIn(key, image.config.labels)
        self.assertEqual(image.config.labels[key], value)

    def test_build_image_from_resource(self):
        repository = GitRepository(
            server=GitServer.HUGGING_FACE_DATASETS,
            owner="JetBrains-Research",
            name="EnvBench",
        )
        resource = repository.head().resource("repos/full/jvm/google__gson.tar.gz")
        request = DownloadRequest(descriptor=resource.descriptor())
        image = self.service.build(
            request=request,
            image_base="alpine-latest",
            image_version=self.test_build_image_from_resource.__name__,
            service_version="test",
            registry=self.registry,
        )
        self.assertImageCorrect(image, request, self.test_build_image_from_resource.__name__)

    def test_create_container_success(self):
        image = self.service.build(
            request=self.request,
            image_base="alpine-latest",
            image_version=self.test_create_container_success.__name__,
            service_version="test",
            registry=self.registry,
        )
        self.images.append(image)

        names = [script.name for script in self.scripts]
        paths = [Path(name) for name in names]
        container = self.service.run(image=image, scripts=paths)
        self.containers.append(container)

        self.assertIsNotNone(container)
        self.assertTrue(container.id)
        self.assertNotEqual(container.network_settings.ports, {})
        self.assertIn(container.state.status, ["created", "running"])

        self.assertEqual(len(container.mounts), len(self.scripts))
        for mount in container.mounts:
            self.assertIn(mount.source, [script.name for script in self.scripts])
            self.assertIn(__CONTAINER_VOLUME_PATH__, mount.destination)
            self.assertEqual(mount.mode, "ro")
            self.assertEqual(mount.type, "bind")


if __name__ == "__main__":
    main()
