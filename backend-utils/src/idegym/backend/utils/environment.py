from os import environ as procenv
from os import pathsep
from typing import Dict

IDEGYM_PATH = "IDEGYM_PATH"
PYTHONPATH = "PYTHONPATH"


def cleanenv() -> Dict[str, str]:
    """
    Return a clean environment for subprocesses.
    Variables and values are copied from the current process' environment,
    while those specific to IdeGYM are removed.

    Returns:
        Dict[str, str]: Cleaned copy of the environment
    """
    env = procenv.copy()
    idegym_path = env.pop(IDEGYM_PATH, "")

    if PYTHONPATH in env:
        python_path = env.pop(PYTHONPATH, "")
        paths = python_path.split(pathsep)
        parts = (path for path in paths if path != idegym_path)
        value = pathsep.join(parts)
        if value:
            env[PYTHONPATH] = value

    return env
