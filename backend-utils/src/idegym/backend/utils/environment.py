from os import environ as procenv
from os import pathsep

IDEGYM_PATH = "IDEGYM_PATH"
PYTHONPATH = "PYTHONPATH"


def cleanenv() -> dict[str, str]:
    """
    Return a copy of the current process environment with IdeGYM-specific entries removed.

    Strips IDEGYM_PATH and removes the IdeGYM path entry from PYTHONPATH so that
    subprocesses do not inherit internal IdeGYM modules.
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
