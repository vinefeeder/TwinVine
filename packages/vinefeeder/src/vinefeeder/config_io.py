from __future__ import annotations
from importlib import resources
import yaml

def read_service_config(service_module) -> dict:
    """
    Read config.yaml that sits NEXT TO the given service module's __init__.py.
    Works for any 'vinefeeder.services.<Name>' package.
    """
    cfg = resources.files(service_module.__package__).joinpath("config.yaml")
    # .open() works whether installed from wheel or editable src
    with cfg.open("rb") as f:
        return yaml.safe_load(f) or {}
