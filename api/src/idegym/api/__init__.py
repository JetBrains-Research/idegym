from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("idegym-api")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"

if __version__ == "0.0.0.dev0":
    __version__ = "latest"
