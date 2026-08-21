from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "hardware.json"


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path or os.environ.get("ROBOT_PROJECT_CONFIG", DEFAULT_CONFIG_PATH))
    config = load_json(config_path)
    config["_config_path"] = str(config_path)
    return config


def runtime_dir(config: dict[str, Any]) -> Path:
    return Path(config["paths"]["runtime_dir"])


def ensure_runtime_dirs(config: dict[str, Any]) -> None:
    root = runtime_dir(config)
    for name in [
        "audio",
        "events",
        "history",
        "locks",
        "media",
        "sessions",
        "task_groups",
    ]:
        (root / name).mkdir(parents=True, exist_ok=True)


def merge_dicts(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(base)
    if not override:
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dicts(result[key], value)
        else:
            result[key] = value
    return result
