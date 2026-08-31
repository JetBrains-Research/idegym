from pathlib import Path
from typing import Optional

from idegym.backend.utils.diff_patch import apply_patch


class FileManager:
    def __init__(self, working_directory: Optional[Path] = None):
        self.working_directory = working_directory

    def create_file(self, file_path: Path, content: str):
        full_file_path = self._calculate_full_file_path(file_path)
        with open(full_file_path, "w", encoding="utf-8") as file:
            file.write(content)

    def edit_file(self, file_path: Path, start_line: int, end_line: int, new_content: str):
        full_file_path = self._calculate_full_file_path(file_path)
        with open(full_file_path, "r", encoding="utf-8") as file:
            lines = file.readlines()

        start_idx = start_line - 1
        end_idx = end_line

        new_lines = lines[:start_idx] + [new_content + "\n"] + lines[end_idx:]

        with open(full_file_path, "w", encoding="utf-8") as file:
            file.writelines(new_lines)

    def patch_file(self, file_path: Path, patch: str):
        full_file_path = self._calculate_full_file_path(file_path)
        with open(full_file_path, "r", encoding="utf-8") as file:
            content = file.read()

        new_content = apply_patch(content, patch)

        with open(full_file_path, "w", encoding="utf-8") as file:
            file.write(new_content)

    def write_chunk(self, file_path: Path, data: bytes, offset: int = 0, truncate: bool = True) -> tuple[int, int]:
        """Write ``data`` at ``offset`` and return ``(bytes_written, size_after_write)``.

        Missing parent directories are created, and ``truncate`` cuts the file off at the end of
        the chunk so re-uploading a shorter file cannot leave the previous tail behind. Writing
        past the current end of the file leaves a hole of zero bytes, as ``lseek`` does.
        """
        full_file_path = Path(self._calculate_full_file_path(file_path))
        full_file_path.parent.mkdir(parents=True, exist_ok=True)
        # "r+b" refuses to create the file and "w+b" would discard an earlier chunk, so pick per call.
        mode = "r+b" if full_file_path.exists() else "w+b"
        with open(full_file_path, mode) as file:
            file.seek(offset)
            bytes_written = file.write(data)
            if truncate:
                file.truncate()
        return bytes_written, full_file_path.stat().st_size

    def read_chunk(self, file_path: Path, offset: int = 0, length: int = 1024 * 1024) -> tuple[bytes, int]:
        """Read at most ``length`` bytes from ``offset`` and return them with the total file size."""
        full_file_path = Path(self._calculate_full_file_path(file_path))
        if not full_file_path.exists():
            raise FileNotFoundError(f"File not found: {full_file_path}")
        if full_file_path.is_dir():
            raise IsADirectoryError(f"Path is a directory, not a file: {full_file_path}")
        size = full_file_path.stat().st_size
        with open(full_file_path, "rb") as file:
            file.seek(offset)
            return file.read(length), size

    def _calculate_full_file_path(self, file_path):
        return file_path if self.working_directory is None else self.working_directory / file_path
