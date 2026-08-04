from pathlib import Path
from typing import Any

from hydra import compose, initialize_config_dir
from idegym.api.config import Config
from omegaconf import OmegaConf

HYDRA_CONFIG_DIR = Path(__file__).parent / "hydra_configs"


def load_config() -> Config:
    """Compose the orchestrator's Hydra configuration into the shared :class:`Config` model.

    Lives apart from ``main`` so the migration CLI can read the same database settings as
    the service without importing the FastAPI application and everything it pulls in.
    """
    with initialize_config_dir(version_base=None, config_dir=str(HYDRA_CONFIG_DIR)):
        cfg = compose(config_name="config")
    container: dict[str, Any] = OmegaConf.to_container(cfg=cfg, resolve=True)
    return Config(**container)
