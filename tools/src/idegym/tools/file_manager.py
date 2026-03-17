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

    def _calculate_full_file_path(self, file_path):
        return file_path if self.working_directory is None else self.working_directory / file_path
