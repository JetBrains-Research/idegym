from enum import StrEnum

API_BASE_PATH = "/api"


class APIPath(StrEnum):
    pass


class ActuatorPath(APIPath):
    """
    API path constants for the actuator API.
    """

    HEALTH = "/health"
    LOG = "/log"
    METRICS = "/metrics"
    SHUTDOWN = "/shutdown"


class FSPath(APIPath):
    """
    API path constants for the filesystem API.
    """

    LIST_DIRECTORY = "/fs/ls"
    READ_FILE = "/fs/cat"
    CREATE_FILE = "/fs/touch"
    CREATE_DIRECTORY = "/fs/mkdir"
    DELETE_PATH = "/fs/rm"


class ProjectPath(APIPath):
    """
    API path constants for the project API.
    """

    RESET = "/project/reset"


class RewardsPath(APIPath):
    """
    API path constants for the rewards API.
    """

    SETUP = "/rewards/setup"
    COMPILATION = "/rewards/compilation"
    TEST = "/rewards/test"


class ToolsPath(APIPath):
    """
    API path constants for the tools API.
    """

    BASH = "/tools/bash"
    CREATE_FILE = "/tools/file/create"
    EDIT_FILE = "/tools/file/edit"
    PATCH_FILE = "/tools/file/patch"
