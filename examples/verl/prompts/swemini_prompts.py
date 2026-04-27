from pathlib import Path

import yaml


def load_prompts(parsing_mode: str, prompts_file: str | Path | None = None) -> dict:
    if prompts_file is None:
        prompts_file = Path(__file__).parent / f"prompts_swemini_{parsing_mode}.yaml"

    with open(prompts_file) as f:
        prompts = yaml.safe_load(f) or {}

    return prompts
