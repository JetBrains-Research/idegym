from pathlib import Path


def get_base_filename(file_path: str | Path) -> str:
    """Return the lowercased stem of a file path, stripping all extensions.

    For example, ``"Archive.tar.gz"`` returns ``"archive"``.
    """
    return Path(file_path).name.lower().split(".")[0]
