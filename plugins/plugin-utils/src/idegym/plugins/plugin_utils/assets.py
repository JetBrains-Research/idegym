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
    (e.g. ``idegym/plugins/plugin_utils/scripts/start-ide.sh``), so they are readable with
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


def ide_context_files(
    package: str,
    prefix: str,
    *,
    install_open_project: bool,
    mcp_steroid: bool,
) -> dict[str, Traversable]:
    """Build-context files an IDE plugin (idea/pycharm) ``COPY``s, mirroring its ``render()`` branches.

    Both the open-project and mcp-steroid-start entrypoints ``COPY`` the shared scripts + supervisord
    config; only open-project also ``COPY``s the per-IDE prebuilt plugin zip. Keys are the ``COPY``
    destination paths (relative to the build context); values are the packaged assets. Shared between
    the idea and pycharm plugins so the two never drift out of sync with each other's ``render()``.

    ``check-mcp.sh``, the ``start-ide.sh`` entrypoint, and the supervisord config are identical for
    both IDEs, so they ship once in ``plugin-utils`` (``__package__`` here) and are ``COPY``d from
    there; only the prebuilt open-project zip lives under the per-IDE ``package``/``prefix``.
    """
    if not (install_open_project or mcp_steroid):
        return {}
    context_files = {
        "plugins/plugin-utils/scripts/check-mcp.sh": plugin_asset(__package__, "scripts", "check-mcp.sh"),
        "plugins/plugin-utils/scripts/start-ide.sh": plugin_asset(__package__, "scripts", "start-ide.sh"),
        "plugins/plugin-utils/scripts/supervisord-ide.conf": plugin_asset(
            __package__, "scripts", "supervisord-ide.conf"
        ),
    }
    if install_open_project:
        context_files[f"{prefix}/project-opener/project-opener.zip"] = plugin_asset(
            package, "project-opener", "project-opener.zip"
        )
    return context_files
