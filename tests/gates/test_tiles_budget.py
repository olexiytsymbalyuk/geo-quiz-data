"""Ворота: тайли першого екрана мапи (z3–z6 над Україною) вкладаються в частку бюджету «< 1 MB»."""

from __future__ import annotations

import json
from pathlib import Path

from tests.gates.gate_support import FIRST_SCREEN_TILES_BUDGET_BYTES


def test_first_screen_tiles_within_budget(dataset_dir: Path) -> None:
    metrics = json.loads((dataset_dir / "metrics.json").read_text(encoding="utf-8"))
    size = metrics["tile_stats"]["first_screen_z3_z6_bytes"]
    assert isinstance(size, int) and size > 0, "metrics.json без first_screen_z3_z6_bytes — перевірка була б порожньою"
    assert size <= FIRST_SCREEN_TILES_BUDGET_BYTES, f"тайли z3–z6 над Україною: {size} B > {FIRST_SCREEN_TILES_BUDGET_BYTES} B"
