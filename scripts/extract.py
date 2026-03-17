#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "tqdm",
# ]
# ///
from argparse import ArgumentParser, Namespace
from os import chmod, fspath
from pathlib import Path
from shutil import copyfileobj
from sys import stderr, stdout
from tarfile import TarFile
from zipfile import ZipFile

from tqdm import tqdm
from tqdm.utils import CallbackIOWrapper


def progressbar(total: int, desc: str):
    return tqdm(
        file=stdout,
        total=total,
        desc=desc,
        bar_format="{desc}: {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        unit="iB",
        unit_scale=True,
        unit_divisor=1024,
    )


def size(archive: TarFile | ZipFile) -> int:
    match archive:
        case tarball if type(archive) is TarFile:
            return sum(getattr(info, "size", 0) for info in tarball.getmembers())
        case zipball if type(archive) is ZipFile:
            return sum(getattr(info, "file_size", 0) for info in zipball.infolist())
        case _:
            return -1


# Helpers to detect and strip a single top-level directory inside the archive


def _single_root_in_tar(tar: TarFile) -> str | None:
    roots: set[str] = set()
    for m in tar.getmembers():
        parts = Path(m.name).parts
        if parts:
            roots.add(parts[0])
            if len(roots) > 1:
                return None
    return next(iter(roots)) if roots else None


def _single_root_in_zip(zipball: ZipFile) -> str | None:
    roots: set[str] = set()
    for info in zipball.infolist():
        parts = Path(info.filename).parts
        if parts:
            roots.add(parts[0])
            if len(roots) > 1:
                return None
    return next(iter(roots)) if roots else None


def _strip_root(parts: tuple[str, ...], root: str | None) -> Path:
    if not parts:
        return Path()
    if root and parts[0] == root:
        parts = parts[1:]
    return Path(*parts)


def untar(archive: TarFile, directory: Path):
    root = _single_root_in_tar(archive)
    with progressbar(total=size(archive), desc="Unarchiving") as progress:
        for info in archive.getmembers():
            parts = Path(info.name).parts
            rel = _strip_root(parts, root)
            if not rel.parts:
                # Root directory entry; skip
                continue
            out_path = directory / rel
            if getattr(info, "size", 0) == 0:
                # Directory or empty file entry
                if getattr(info, "type", None) is not None and info.isdir():
                    out_path.mkdir(parents=True, exist_ok=True)
                else:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    # Create empty file
                    with open(fspath(out_path), "wb") as file:
                        pass
                    chmod(fspath(out_path), info.mode)
            else:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with (
                    archive.extractfile(info) as source,
                    open(file=fspath(out_path), mode="wb") as file,
                ):
                    copyfileobj(CallbackIOWrapper(progress.update, source), file)
                chmod(fspath(out_path), info.mode)


def unzip(archive: ZipFile, directory: Path):
    root = _single_root_in_zip(archive)
    with progressbar(total=size(archive), desc="Unarchiving") as progress:
        for info in archive.infolist():
            parts = Path(info.filename).parts
            rel = _strip_root(parts, root)
            if not rel.parts:
                continue
            out_path = directory / rel
            if getattr(info, "file_size", 0) == 0:
                # Directory entry or empty file
                if info.is_dir():
                    out_path.mkdir(parents=True, exist_ok=True)
                else:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(fspath(out_path), "wb") as file:
                        pass
                    if info.external_attr > 0:
                        mode = info.external_attr >> 16
                        if mode:
                            chmod(fspath(out_path), mode)
            else:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with (
                    archive.open(info) as source,
                    open(file=fspath(out_path), mode="wb") as file,
                ):
                    copyfileobj(CallbackIOWrapper(progress.update, source), file)
                if info.external_attr > 0:
                    mode = info.external_attr >> 16
                    if mode:
                        chmod(fspath(out_path), mode)


# https://stackoverflow.com/a/65513860/17173324


def extract(archive: Path, directory: Path):
    # Use appropriate interface to decompress archive
    match str(archive).lower():
        case name if name.endswith(".tar.gz"):
            with TarFile.open(archive, "r:*") as tarball:
                untar(tarball, directory)
        case name if name.endswith(".zip"):
            with ZipFile(archive, "r") as zipball:
                unzip(zipball, directory)
        case _:
            raise ValueError(f"Unrecognized archive format: {archive}")


def main(args: Namespace):
    try:
        extract(args.archive, args.directory)
    except Exception as ex:
        print(ex, file=stderr)
        exit(1)


if __name__ == "__main__":
    parser = ArgumentParser(
        prog="extract",
        description="Decompress an archive based on its type",
        epilog="Supports only tarball (.tar.gz) and zipball (.zip) archive types.",
    )
    parser.add_argument(
        "archive",
        help="Path to the archive",
        type=Path,
    )
    parser.add_argument(
        "directory",
        help="Path to the destination directory",
        type=Path,
    )
    main(args=parser.parse_args())
