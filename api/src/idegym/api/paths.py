from enum import StrEnum

API_BASE_PATH = "/api"


class APIPath(StrEnum):
    pass


class ActuatorPath(APIPath):
    HEALTH = "/health"
    LOG = "/log"
    METRICS = "/metrics"
    SHUTDOWN = "/shutdown"


class OpenenvPath(APIPath):
    """Path constants for OpenEnv servers deployed alongside IdeGYM without the IdeGYM API."""

    HEALTH = "/health"


class FSPath(APIPath):
    LIST_DIRECTORY = "/fs/ls"
    READ_FILE = "/fs/cat"
    CREATE_FILE = "/fs/touch"
    CREATE_DIRECTORY = "/fs/mkdir"
    DELETE_PATH = "/fs/rm"


class ProjectPath(APIPath):
    RESET = "/project/reset"


class RewardsPath(APIPath):
    SETUP = "/rewards/setup"
    COMPILATION = "/rewards/compilation"
    TEST = "/rewards/test"


class ToolsPath(APIPath):
    BASH = "/tools/bash"
    CREATE_FILE = "/tools/file/create"
    EDIT_FILE = "/tools/file/edit"
    PATCH_FILE = "/tools/file/patch"
