"""
Image builder public API.

Core types:
- ``Image`` — fluent builder for container images; serialize/deserialize via YAML or dict.
- ``PluginBase`` — base class for image build plugins; subclass and register with ``@image_plugin``.
- ``BuildContext`` — immutable state passed through the plugin pipeline.
- ``image_plugin`` — decorator to register a ``PluginBase`` subclass under a type name.
- ``Base`` — enum of well-known base images (alias for ``BaseImage``).

Built-in plugins are in ``idegym.plugins.defaults`` and are auto-registered when the image
builder is imported (via the ``idegym.plugins.image`` entry point group).
"""

from importlib.metadata import PackageNotFoundError, version

from idegym.api.docker import BaseImage as Base
from idegym.api.plugin import BuildContext, PluginBase, image_plugin
from idegym.image.builder import Image

try:
    __version__ = version("idegym-image-builder")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"

if __version__ == "0.0.0.dev0":
    __version__ = "latest"

__all__ = (
    "__version__",
    "Base",
    "BuildContext",
    "Image",
    "PluginBase",
    "image_plugin",
)
