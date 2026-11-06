"""Optional ``config.toml`` next to the scripts. Every key has a default, so the file is never required."""

from __future__ import annotations

import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "ui": {"theme": "", "layout": "auto", "poll_hz": 5.0},
    "edu": {"port": "", "min_battery": 40},
    "sim": {"cm_per_s": 80.0, "vertical_cm_per_s": 60.0, "verify": True},
    "pilot": {"speed": 2, "rate_hz": 20.0, "hold": "auto"},
    "log": {"commands": True},
}


def load_config(path: Path | None = None) -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    p = path or PROJECT_ROOT / "config.toml"
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, tomllib.TOMLDecodeError) as exc:
        cfg["_error"] = f"{p.name}: {exc}"
        return cfg
    for section, values in data.items():
        if isinstance(values, dict):
            cfg.setdefault(section, {}).update(values)
    return cfg
