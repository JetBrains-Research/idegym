"""Locate a plugin's build-time assets (scripts, configs, prebuilt zips).

These files are ``COPY``-ed into images by the idea/pycharm plugins. They ship in the
``idegym-plugins`` wheel (see the ``force-include`` block in ``plugins/pyproject.toml``) so a
plugin author never needs a checkout of the idegym repo to build an image.
"""

from importlib.resources import files
from importlib.resources.abc import Traversable


def plugin_asset(package: str, *parts: str) -> Traversable:
    """Return a ``Traversable`` for a packaged plugin build asset.

    Assets ship in the wheel via hatchling ``force-include`` under the plugin package
    (e.g. ``idegym/plugins/idea/scripts/start-idea.sh``), so they are readable with
    ``importlib.resources`` from a regular install.

    In an *editable* checkout ``force-include`` does not apply, so fall back to the plugin's
    source tree at the repo path ``plugins/<name>/<parts>`` — four directories above the
    package dir ``plugins/<name>/src/idegym/plugins/<name>``.

    Args:
        package: Dotted package name of the plugin (pass ``__package__``).
        parts: Path segments of the asset relative to the package (e.g. ``"scripts", "x.sh"``).
    """
    root = files(package)
    packaged = root.joinpath(*parts)
    if packaged.is_file():
        return packaged
    source = root
    for _ in range(4):
        source = source.parent
    return source.joinpath(*parts)
