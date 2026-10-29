"""Ворота: у нейтральній суші (росія, Білорусь) немає іменованих фіч, країн і рядків газетира."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.policy import load_policy

from tests.gates.gate_support import (
    Gazetteer,
    PolicyView,
    TileSet,
    neutral_country_layer_violations,
    neutral_geometry_violations,
    find_one,
    neutral_row_violations,
    summarize,
)
from tests.gates.geo_support import NE_WINDOW, NEUTRAL_POINT_TOLERANCE_KM, neutral_point_violations, ne_union


def test_gazetteer_has_no_neutral_country_rows(gazetteer: Gazetteer, policy: PolicyView) -> None:
    assert policy.neutral_countries
    assert gazetteer.entities, "у газетирі немає сутностей — перевірка була б порожньою"
    found = neutral_row_violations(gazetteer, policy.neutral_countries)

    assert not found, summarize(found)


def test_gazetteer_points_stay_out_of_neutral_land(gazetteer: Gazetteer, raw_dir: Path) -> None:
    """`country` у газетирі завжди UA, тож країну не перевірити: перевіряються координати проти NE (RU, BY)."""
    ne_shp = find_one(raw_dir / "ne_admin0_ukr", "*.shp")
    neutral = ne_union(ne_shp, NE_WINDOW, load_policy().neutral_countries)
    assert not neutral.is_empty, "у NE немає нейтральних країн — перевірка була б порожньою"

    found, checked = neutral_point_violations(gazetteer, neutral, NEUTRAL_POINT_TOLERANCE_KM * 1000.0)

    assert checked > 0, "у газетирі немає жодної сутності з координатами — перевірка порожня"
    assert not found, summarize(found)


def test_country_layer_has_no_neutral_country(tiles: TileSet, dataset_dir: Path) -> None:
    metrics = json.loads((dataset_dir / "metrics.json").read_text(encoding="utf-8"))
    neutral_ids = frozenset(metrics["neutral_country_ids"])
    assert neutral_ids, "metrics.json без neutral_country_ids — перевірка була б порожньою"

    assert tiles.attributes, "у PMTiles немає жодної фічі — перевірка була б порожньою"
    found = neutral_country_layer_violations(tiles.attributes, neutral_ids)

    assert not found, summarize(found)


def test_no_named_feature_inside_neutral_landmass(tiles: TileSet) -> None:
    found, checked = neutral_geometry_violations(tiles)

    assert checked > 0, "у вибірці немає жодного тайла з нейтральною сушею — перевірка порожня"
    assert not found, summarize(found)
