#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "from-root",
#   "python-on-whales",
# ]
# ///
from argparse import ArgumentParser, Namespace
from pathlib import Path
from shutil import which
from subprocess import run
from sys import stderr, stdout
from typing import Iterable

from from_root import from_root
from python_on_whales import DockerClient

docker = DockerClient()


def build(
    context: Path,
    file: Path,
    registry: str,
    versions: Iterable[str],
    cache: bool,
    push: bool,
    multiplatform: bool,
    skip_minikube: bool = False,
):
    tags = [f"{registry}/orchestrator:{version}" for version in versions]
    platforms = ["linux/amd64", "linux/arm64"] if multiplatform else None
    for line in docker.build(
        file=file,
        context_path=context,
        tags=tags,
        platforms=platforms,
        cache=cache,
        push=push,
        progress="plain",
        stream_logs=True,
    ):
        if line := line.strip():
            print(line)
    if skip_minikube or not which("minikube"):
        return
    process = run(
        args=["minikube", "image", "load", *tags, "-v=2", "--overwrite", "--alsologtostderr"],
        capture_output=True,
        check=True,
        text=True,
    )
    if output := process.stdout.strip():
        for line in output.splitlines():
            print(line, file=stdout)
    if errors := process.stderr.strip():
        for line in errors.splitlines():
            print(line, file=stderr)


def main(args: Namespace):
    build(
        context=args.context,
        file=args.file,
        registry=args.registry,
        versions=args.versions,
        cache=not args.no_cache,
        push=args.push,
        multiplatform=args.multiplatform,
        skip_minikube=args.skip_minikube,
    )


if __name__ == "__main__":
    parser = ArgumentParser(
        prog="build-orchestrator-image",
        description="Build IdeGYM orchestrator image",
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
        "-f",
        "--file",
        help="Path to the Dockerfile",
        default=from_root("orchestrator", "Dockerfile"),
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
        help="Version to use in the built image tag",
        default=["latest"],
        nargs="+",
        type=str,
    )
    parser.add_argument(
        "--multiplatform",
        help="Build the image for multiple platforms",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--no-cache",
        help="Do not use cache when building the image",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--push",
        help="Push the built image to the registry",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--skip-minikube",
        help="Skip loading the image into minikube",
        action="store_true",
        default=False,
    )
    main(args=parser.parse_args())
