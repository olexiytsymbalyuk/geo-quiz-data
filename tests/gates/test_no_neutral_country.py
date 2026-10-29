"""Ворота: у нейтральній суші (росія, Білорусь) немає іменованих фіч, країн і рядків газетира."""

from __future__ import annotations

import json
from pathlib import Path

from tests.gates.gate_support import (
    Gazetteer,
    PolicyView,
    TileSet,
    neutral_country_layer_violations,
    neutral_geometry_violations,
    neutral_row_violations,
    summarize,
)


def test_gazetteer_has_no_neutral_country_rows(gazetteer: Gazetteer, policy: PolicyView) -> None:
    assert policy.neutral_countries
    found = neutral_row_violations(gazetteer, policy.neutral_countries)

    assert not found, summarize(found)


def test_country_layer_has_no_neutral_country(tiles: TileSet, dataset_dir: Path) -> None:
    metrics = json.loads((dataset_dir / "metrics.json").read_text(encoding="utf-8"))
    neutral_ids = frozenset(metrics["neutral_country_ids"])
    assert neutral_ids, "metrics.json без neutral_country_ids — перевірка була б порожньою"

    found = neutral_country_layer_violations(tiles.attributes, neutral_ids)

    assert not found, summarize(found)


def test_no_named_feature_inside_neutral_landmass(tiles: TileSet) -> None:
    found, checked = neutral_geometry_violations(tiles)

    assert checked > 0, "у вибірці немає жодного тайла з нейтральною сушею — перевірка порожня"
    assert not found, summarize(found)
