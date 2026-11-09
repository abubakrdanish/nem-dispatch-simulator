"""Settings from config/settings.yml, with paths resolved against the repository root."""
from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@cache
def settings() -> dict:
    return yaml.safe_load((ROOT / "config" / "settings.yml").read_text())


def path(key: str) -> Path:
    return (ROOT / settings()[key]).resolve()
