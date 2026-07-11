from __future__ import annotations

from pathlib import Path
import os
import yaml
from importlib import resources
from typing import Tuple, Optional, Dict, Any


# Optional: allow a one-line override without changing code.
# If VINEFEEDER_CONFIG is set, we'll use that file.
ENV_VAR = "VINEFEEDER_CONFIG"
PKG_NAME = "vinefeeder"
CFG_NAME = "config.yaml"


def project_config_path() -> Path:
    """
    Resolve the config file path with this priority:
      1) $VINEFEEDER_CONFIG if set
      2) <package_dir>/config.yaml  (the repo copy beside the code)
      3) importlib.resources fallback (works when installed as a wheel)
    """
    # 1) explicit override via env
    env_path = os.environ.get(ENV_VAR)
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if p.exists():
            return p

    # 2) the config.yaml living next to this file (repo/dev layout)
    here = Path(__file__).resolve().parent
    local_cfg = here / CFG_NAME
    if local_cfg.exists():
        return local_cfg

    # 3) installed package data (site-packages / wheel)
    try:
        # resources.files returns a Traversable
        f = resources.files(PKG_NAME).joinpath(CFG_NAME)
        with resources.as_file(f) as p:
            if p.exists():
                return p
    except Exception:
        pass

    # If we get here, we didn’t find it. Return where it *should* be in dev.
    return local_cfg


def load_config_with_fallback() -> Tuple[Dict[str, Any], Optional[Path]]:
    """
    Load YAML config from the resolved project path.
    Returns (config_dict, path_used).
    """
    cfg_path = project_config_path()
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        return data, cfg_path
    except FileNotFoundError:
        # Consistent shape even if missing
        return {}, None


def save_project_config(cfg: Dict[str, Any]) -> Path:
    """
    Save back to the *project* config path (beside the code).
    Useful during development; may fail if running from a read-only install.
    """
    p = project_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, default_flow_style=False), encoding="utf-8")
    return p


# Convenience helpers if you want one-liners elsewhere:
def get_bool(key: str, default: bool = False) -> bool:
    cfg, _ = load_config_with_fallback()
    return bool(cfg.get(key, default))
