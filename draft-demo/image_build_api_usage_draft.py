from pathlib import Path
from tempfile import TemporaryDirectory

from idegym.image import Image
from idegym.image.plugins import BaseSystem, IdegymServer, User


def build_image() -> Image:
    return (
        Image.from_base("debian:bookworm-slim")
        .named("idegym-server-local-draft")
        .with_plugin(BaseSystem())
        .with_plugin(
            User(
                username="appuser",
                uid=1000,
                gid=1000,
                home="/home/appuser",
                shell="/bin/bash",
                sudo=True,
            )
        )
        .with_plugin(IdegymServer.from_local(root=""))
    )


def main() -> None:
    image = build_image()

    yaml_content = image.to_yaml()
    compiled = image.compile()

    print("=== YAML ===")
    print(yaml_content)

    print("=== Dockerfile ===")
    print(compiled.dockerfile_content)

    print("=== Build Metadata ===")
    print(f"name: {compiled.name}")
    print(f"base: {image.base}")
    print(f"image_version: {compiled.image_version()}")
    print(f"context_path: {compiled.context_path}")
    print(f"runtime_class_name: {compiled.runtime_class_name}")
    print(f"project_request: {compiled.request}")

    with TemporaryDirectory(prefix="idegym-image-build-") as directory:
        root = Path(directory)
        yaml_path = root / "images.yaml"
        dockerfile_path = root / "Dockerfile"

        yaml_path.write_text(yaml_content)
        dockerfile_path.write_text(compiled.dockerfile_content)

        print("=== Written Files ===")
        print(yaml_path)
        print(dockerfile_path)

        # Local Docker build usage:
        from idegym.client.docker_api import IdeGYMDockerAPI

        docker_api = IdeGYMDockerAPI()
        docker_api.build_and_push_from_yaml(
            path=yaml_path,
            push=False,
        )


if __name__ == "__main__":
    main()
