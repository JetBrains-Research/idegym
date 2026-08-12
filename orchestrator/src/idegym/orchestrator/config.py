from idegym.api.config import Config
from idegym.backend.utils.settings import ORCHESTRATOR_SECTIONS
from idegym.backend.utils.settings import load_config as load_settings


def load_config() -> Config:
    """Build the orchestrator's :class:`Config` from the environment.

    Apart from ``main`` so the migration CLI can read the same database settings without
    importing the FastAPI application and everything behind it. Binding
    ``ORCHESTRATOR_SECTIONS`` here keeps the section set in one place.
    """
    return load_settings(sections=ORCHESTRATOR_SECTIONS)
