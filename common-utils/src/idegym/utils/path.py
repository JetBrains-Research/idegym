from pathlib import Path
from typing import Union


def get_base_filename(file_path: Union[str, Path]) -> str:
    if isinstance(file_path, str):
        file_path = Path(file_path)
    return file_path.name.lower().split(".")[0]
