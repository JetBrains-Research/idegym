#!/usr/bin/env -S uv run --project image-builder python
from argparse import ArgumentParser, Namespace
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from os import environ as env
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, NamedTuple, Set

from jinja2 import Template
from python_on_whales import DockerClient

docker = DockerClient()
truthy = {"1", "t", "y", "true", "yes"}

# https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/store-information-in-variables
ci = env.get("CI", "False").lower() in truthy
gha = env.get("GITHUB_ACTIONS", "False").lower() in truthy

cache_from = "type=gha" if (ci and gha) else None
cache_to = "type=gha,mode=max" if (ci and gha) else None
images = {
    "debian": "bookworm-20250520-slim",  # 12.11-slim
    "ubuntu": "jammy-20250530",  # 22.04
}
platforms = {"linux/amd64", "linux/arm64"}


class BaseImage(NamedTuple):
    name: str
    tag: str


@dataclass
class BuildArgs:
    template: Template
    context: Path
    registry: str
    base: BaseImage
    versions: Iterable[str]
    push: bool


def build(args: BuildArgs):
    name = f"server-{args.base.name}-{args.base.tag}"
    qualified = f"{args.registry}/{name}"
    tags = [f"{qualified}:{version}" for version in args.versions]
    with NamedTemporaryFile(mode="w", prefix="Dockerfile.") as temporary:
        content = args.template.render(
            image=args.base.name,
            tag=args.base.tag,
        )
        temporary.write(content)
        temporary.flush()
        for line in docker.build(
            file=temporary.name,
            context_path=args.context,
            tags=tags,
            platforms=platforms,
            cache_from=cache_from,
            cache_to=cache_to,
            progress="plain",
            stream_logs=True,
            push=args.push,
        ):
            if line := line.strip():
                print(line)


def main(args: Namespace):
    context: Path = args.context
    registry: str = args.registry
    versions: Set[str] = args.versions
    push: bool = args.push
    skip_base: Set[str] = args.skip_base

    with open(args.template, "r") as buffer:
        content: str = buffer.read()
        template: Template = Template(content)

    bases = [BaseImage(name, tag) for name, tag in images.items() if name not in set(skip_base)]
    builds = [
        BuildArgs(
            template=template,
            context=context,
            registry=registry,
            base=base,
            versions=versions,
            push=push,
        )
        for base in bases
    ]

    if len(builds) < 1:
        exit()

    with ThreadPoolExecutor(
        thread_name_prefix="idegym-build-base-image",
        max_workers=len(builds),
    ) as executor:
        futures = [executor.submit(build, args) for args in builds]
        [future.result() for future in futures]


if __name__ == "__main__":
    parser = ArgumentParser(
        prog="build-server-images",
        description="Build IdeGYM server images from template",
        epilog="To specify the value for a flag, you can either use --flag=[VALUE] or --flag [VALUE]",
    )
    parser.add_argument(
        "-c",
        "--context",
        help="Path to the context directory",
        default=Path.cwd(),
        type=Path,
    )
    parser.add_argument(
        "-t",
        "--template",
        help="Path to the `.jinja` template",
        default=Path("image-builder/src/idegym/image/templates/server.Dockerfile.jinja"),
        type=Path,
    )
    parser.add_argument(
        "-r",
        "--registry",
        help="Name of the Docker registry",
        default="ghcr.io/jetbrains-research/idegym",
        type=str,
    )
    parser.add_argument(
        "-v",
        "--versions",
        help="IdeGYM version to use in the built image tag",
        default={"latest"},
        nargs="+",
        type=str,
    )
    parser.add_argument(
        "-p",
        "--push",
        help="Push the built images to the Docker registry",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--skip-base",
        help="Base image distros to skip in the build",
        choices=set(images.keys()),
        default=set(),
        nargs="*",
        type=str,
    )
    main(args=parser.parse_args())
