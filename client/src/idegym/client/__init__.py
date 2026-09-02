from importlib.metadata import PackageNotFoundError, version

from idegym.client.client import IdeGYMClient
from idegym.client.exceptions import (
    IdeGYMAuthError,
    IdeGYMBadRequestError,
    IdeGYMBusyError,
    IdeGYMCancelledError,
    IdeGYMHTTPError,
    IdeGYMNotFoundError,
    IdeGYMServerError,
    IdeGYMTimeoutError,
)
from idegym.client.shared import SharedIdeGYMClient

try:
    __version__ = version("idegym-client")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"

if __version__ == "0.0.0.dev0":
    __version__ = "latest"

__all__ = (
    "IdeGYMAuthError",
    "IdeGYMBadRequestError",
    "IdeGYMBusyError",
    "IdeGYMCancelledError",
    "IdeGYMClient",
    "IdeGYMHTTPError",
    "IdeGYMNotFoundError",
    "IdeGYMServerError",
    "IdeGYMTimeoutError",
    "SharedIdeGYMClient",
    "__version__",
)
