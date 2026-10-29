"""Шляхи репозиторію й формат версії датасету."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
RAW = ROOT / "data" / "raw"
WORK = ROOT / "data" / "work"

_VERSION_RE = re.compile(r"^\d{4}\.(0[1-9]|1[0-2])\.\d+$")


def validate_version(v: str) -> str:
    """Версія датасету `YYYY.MM.N`; інакше `ValueError` (захист і від `../` у шляху)."""
    if not _VERSION_RE.fullmatch(v):
        raise ValueError(f"версія датасету має вигляд YYYY.MM.N, отримано {v!r}")
    return v


def dist_dir(version: str) -> Path:
    """Тека артефактів релізу `dist/<версія>/`."""
    return ROOT / "dist" / validate_version(version)
