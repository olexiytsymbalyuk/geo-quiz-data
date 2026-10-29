"""Ворота: державний кордон повний (Додаток Б, `test_borders_full`, з уточненнями спеки §6).

`level=0` — лише сухопутний кордон, без берега, тож він не замкнений. Умова брифу «один замкнений контур»
перекладена на три перевірки: (a) контур `land` — один основний полігон без дірок і острови; (b) кордон без розривів
уздовж суші сусідів; (c) відхилення від NE `_ukr` ≤ 2 км по сухопутних ділянках (поріг не послаблюється).
Усе — на z12, найдетальнішому зумі: на низьких зумах (z3–z4) спрощення замикає лагуни (Сиваш) і дає 18 хибних дірок.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import shapely
from shapely.geometry.base import BaseGeometry

from tests.gates.gate_support import TileSet, find_one, summarize
from tests.gates.geo_support import (
    FINEST_ZOOM,
    MAX_DEVIATION_KM,
    STATE_BORDER_LEVEL,
    border_deviation,
    border_gaps,
    land_mask_metric,
    layer_union,
    ne_countries,
    neighbour_union,
    outline_violations,
)


@pytest.fixture(scope="module")
def land(tiles: TileSet) -> BaseGeometry:
    union = layer_union(tiles, "land", FINEST_ZOOM)
    assert not union.is_empty, "у PMTiles немає шару land — перевірка була б порожньою"
    return union


@pytest.fixture(scope="module")
def neighbours(tiles: TileSet) -> BaseGeometry:
    union = neighbour_union(tiles, FINEST_ZOOM)
    assert not union.is_empty, "у PMTiles немає ні landmass, ні country — перевірка була б порожньою"
    return union


@pytest.fixture(scope="module")
def border(tiles: TileSet) -> BaseGeometry:
    union = layer_union(tiles, "admin_line", FINEST_ZOOM, level=STATE_BORDER_LEVEL)
    assert not union.is_empty, "у PMTiles немає admin_line рівня 0 — перевірка була б порожньою"
    return union


def test_land_outline_is_one_polygon(land: BaseGeometry, neighbours: BaseGeometry) -> None:
    found, parts = outline_violations(land, neighbours)

    assert parts > 0
    assert not found, summarize(found)


def test_state_border_has_no_gaps(
    land: BaseGeometry, neighbours: BaseGeometry, border: BaseGeometry, raw_dir: Path
) -> None:
    zip_path = find_one(raw_dir / "land_polygons", "*.zip")
    found = border_gaps(land, neighbours, border, lambda bbox: land_mask_metric(zip_path, bbox))

    assert not found, summarize(found)


def test_border_matches_natural_earth(
    neighbours: BaseGeometry, border: BaseGeometry, raw_dir: Path, gates_out: Path
) -> None:
    ne_home, ne_neighbours = ne_countries(find_one(raw_dir / "ne_admin0_ukr", "*.shp"))
    assert not ne_home.is_empty and not ne_neighbours.is_empty, "в NE немає України чи сусідів"
    report = border_deviation(border, neighbours, ne_home, ne_neighbours)
    (gates_out / "border-deviation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    assert report["overall"]["points"] > 0, "немає жодної сухопутної ділянки кордону — перевірка порожня"
    assert report["overall"]["max"] <= MAX_DEVIATION_KM, (
        f"відхилення від NE: p50 {report['overall']['p50']} км, p95 {report['overall']['p95']} км, "
        f"max {report['overall']['max']} км (поріг {MAX_DEVIATION_KM} км); рішення — за власником"
    )
