from idegym.api.config import Config
from idegym.backend.utils.settings import WATCHER_SECTIONS
from idegym.backend.utils.settings import load_config as load_settings


def load_config() -> Config:
    """Build the watcher's :class:`Config` from the environment."""
    return load_settings(sections=WATCHER_SECTIONS)
