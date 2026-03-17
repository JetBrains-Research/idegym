from importlib.metadata import PackageNotFoundError, version

from idegym.client.client import IdeGYMClient
from idegym.client.docker_api import IdeGYMDockerAPI

try:
    __version__ = version("idegym-client")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"

if __version__ == "0.0.0.dev0":
    __version__ = "latest"

__all__ = (
    "__version__",
    "IdeGYMClient",
    "IdeGYMDockerAPI",
)
