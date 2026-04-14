"""
Image builder public API.

Core types:
- ``Image`` — fluent builder for container images; serialize/deserialize via YAML or dict.
- ``PluginBase`` — base class for image build plugins; subclass and register with ``@image_plugin``.
- ``BuildContext`` — immutable state passed through the plugin pipeline.
- ``image_plugin`` — decorator to register a ``PluginBase`` subclass under a type name.
- ``Base`` — enum of well-known base images (alias for ``BaseImage``).

Built-in plugins are in ``idegym.image.plugins`` and are auto-registered on import of this package.
"""

from idegym.api.docker import BaseImage as Base
from idegym.image.builder import Image
from idegym.image.plugin import BuildContext, PluginBase, image_plugin

__all__ = ("Base", "BuildContext", "Image", "PluginBase", "image_plugin")
