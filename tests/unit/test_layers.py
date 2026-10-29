"""Шари тайлів: білий список атрибутів, мінзуми, кліп по контуру (Завдання 11)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.geometry.base import BaseGeometry

from pipeline.gazetteer import Entity, GazetteerResult, read_layer
from pipeline.geo_types import Feature
from pipeline.layers import (
    KYIV_CODE,
    LAYER_WHITELIST,
    SEVASTOPOL_CODE,
    WhitelistError,
    build_layers,
    make_feature,
)

OUTLINE = MultiPolygon([box(0, 0, 10, 10)])
BORDER = MultiLineString([[(0, 10), (10, 10)]])
FORBIDDEN = "б\u044b"  # українська «б» + заборонена літера (escape ставить sed)


def _entity(entity_id: str, type_: str, name: str, **kwargs: Any) -> Entity:
    return Entity(id=entity_id, type=type_, names={"uk": name}, **kwargs)


def _settlement(
    code: str, type_: str, name: str, tier: int, lat: float | None, population: int | None = None, *, centre: bool = False
) -> Entity:
    return _entity(
        f"ua:{code}",
        type_,
        name,
        katottg=code,
        lat=lat,
        lon=None if lat is None else 5.0,
        tiers={"ukraine": tier},
        population=None if population is None else {"value": population, "year": None, "source": "osm"},
        attributes={"is_oblast_centre": centre},
    )


def _poly_entity(code: str, type_: str, name: str, relation_id: int | None) -> Entity:
    return _entity(
        f"ua:{code}",
        type_,
        name,
        katottg=code,
        geom_ref=None if relation_id is None else f"osm:r{relation_id}",
    )


def _relation(relation_id: int, geometry: BaseGeometry, **props: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"@type": "relation", "@id": relation_id, **props},
        "geometry": mapping(geometry),
    }


def _way(way_id: int, geometry: BaseGeometry, **props: str) -> dict[str, Any]:
    return {"type": "Feature", "properties": {"@type": "way", "@id": way_id, **props}, "geometry": mapping(geometry)}


def _write(path: Path, features: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(f, ensure_ascii=False) + "\n" for f in features), encoding="utf-8")


ADMIN_FEATURES = [
    _relation(1, box(-2, 0, 6, 10), katotth="UA01000000000000001"),  # область виходить за контур
    _relation(2, box(-1, -1, 5, 11), katotth="UA01020000000000002"),  # район
    _relation(3, box(-1, -1, 5, 11), katotth="UA01020010000000003"),  # громада 1
    _relation(4, box(5, -1, 11, 11), katotth="UA01020010000000004"),  # громада 2: спільна межа x = 5
    _relation(5, box(7, 2, 9, 4), katotth=KYIV_CODE),  # полігон K — за katotth, не за geom_ref
]

LAKE_BIG = box(2, 2, 2.06, 2.06)  # ≈ 44 км²
LAKE_SMALL = box(3, 3, 3.03, 3.03)  # ≈ 11 км²
WATER_FEATURES = [
    _way(10, LAKE_BIG, natural="water", **{"name:uk": "Велике", "wikidata": "Q10"}),
    _way(11, LAKE_SMALL, natural="water", **{"name:uk": "Мале"}),
    _way(12, box(4, 4, 4.06, 4.06), natural="water"),  # без назви й не в газетирі
    _way(13, LineString([(5, 5), (15, 5)]), waterway="river", **{"name:uk": "Виходить"}),  # кліпається до x ≤ 10
    _way(14, LineString([(1, 6), (1.6, 6)]), waterway="river", **{"name:uk": "Два шматки", "wikidata": "Q14"}),
    _way(15, LineString([(2, 7), (2.6, 7)]), waterway="river", **{"name:uk": "Два шматки", "wikidata": "Q14"}),
    _way(16, LineString([(1, 8), (1.5, 8)]), waterway="river", **{"name:uk": "Коротка"}),
    _way(17, LineString([(20, 20), (21, 20)]), waterway="river", **{"name:uk": "Далеко"}),  # увесь поза контуром
]

CENTRE = "UA01020010010000010"
BIG_CITY = "UA01020010010000011"
TWIN_CITY = "UA01020010010000012"
SELYSHCHE = "UA01020010010000013"
SELO = "UA01020010010000014"
SELO_NO_POINT = "UA01020010010000015"
OBLAST = "UA01000000000000001"
RAION = "UA01020000000000002"
HROMADA_1 = "UA01020010000000003"
HROMADA_2 = "UA01020010000000004"
HROMADA_NO_POLYGON = "UA01020010000000009"


def _gazetteer() -> GazetteerResult:
    entities = [
        _poly_entity(OBLAST, "oblast", "Область", 1),
        _poly_entity(RAION, "raion", "Район", 2),
        _poly_entity(HROMADA_1, "hromada", "Громада 1", 3),
        _poly_entity(HROMADA_2, "hromada", "Громада 2", 4),
        _poly_entity(HROMADA_NO_POLYGON, "hromada", "Без полігона", None),
        _settlement(KYIV_CODE, "city", "Київ", 1, 7.0, 2_900_000, centre=True),
        _settlement(SEVASTOPOL_CODE, "city", "Севастополь", 1, 3.0, 485_000),
        _settlement(CENTRE, "city", "Обласний центр", 1, 4.0, 300_000, centre=True),
        _settlement(BIG_CITY, "city", "Велике місто", 2, 5.0, 60_000),
        _settlement(TWIN_CITY, "city", "Інше місто", 2, 5.5, 60_000),
        _settlement(SELYSHCHE, "selyshche", "Селище", 3, 6.0, 3_000),
        _settlement(SELO, "selo", "Село", 4, 6.5, None),
        _settlement(SELO_NO_POINT, "selo", "Село без точки", 4, None),
        _entity("wd:Q10", "lake", "Велике", qid="Q10"),
        _entity("osm:w11", "lake", "Мале"),
        _entity("osm:w13", "river", "Виходить"),
        _entity("wd:Q14", "river", "Два шматки", qid="Q14"),
        _entity("osm:w16", "river", "Коротка"),
        _entity("osm:w17", "river", "Далеко"),
        _entity("wd:Q99", "peak", "Говерла", lat=8.0, lon=8.0, attributes={"ele": 2061}),
        _entity("wd:Q98", "peak", "Без висоти", lat=8.5, lon=8.5),
    ]
    return GazetteerResult(entities, {}, {}, "2026.11.1")


@pytest.fixture
def osm_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "extract"
    directory.mkdir()
    _write(directory / "admin.geojsonl", ADMIN_FEATURES)
    _write(directory / "water.geojsonl", WATER_FEATURES)
    return directory


NEUTRAL_LAND: list[Feature] = [{"geometry": MultiPolygon([box(10, 0, 12, 10)]), "properties": {"class": "neutral"}}]
COUNTRIES: list[Feature] = [
    {"geometry": MultiPolygon([box(-2, 0, 0, 10)]), "properties": {"id": "wd:Q36", "name:uk": "Польща"}},
    {"geometry": MultiPolygon([box(0, -2, 10, 0)]), "properties": {"id": "wd:Q219"}},
]


@pytest.fixture
def layers(tmp_path: Path, osm_dir: Path) -> dict[str, list[dict[str, Any]]]:
    paths = build_layers(_gazetteer(), OUTLINE, BORDER, NEUTRAL_LAND, COUNTRIES, osm_dir, out_dir=tmp_path / "layers")
    return {name: list(read_layer(path)) for name, path in paths.items()}


def _by_id(features: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f["properties"]["id"]: f for f in features}


def _zoom(feature: dict[str, Any]) -> int:
    zoom = feature["tippecanoe"]["minzoom"]
    assert isinstance(zoom, int)
    return zoom


# --- білий список ---


def test_whitelist_matches_the_spec_table() -> None:
    assert dict(LAYER_WHITELIST) == {
        "landmass": frozenset({"class"}),
        "country": frozenset({"id", "name:uk"}),
        "land": frozenset(),
        "admin": frozenset({"id", "level", "name:uk"}),
        "admin_line": frozenset({"level"}),
        "place": frozenset({"id", "name:uk", "kind", "rank"}),
        "water": frozenset({"id", "name:uk", "kind"}),
        "waterway": frozenset({"id", "name:uk", "kind"}),
        "peak": frozenset({"id", "name:uk", "ele"}),
    }


def test_make_feature_rejects_bare_name_key() -> None:
    with pytest.raises(WhitelistError):
        make_feature("place", Point(1, 1), {"id": "ua:x", "name:uk": "Ялта", "name": "Ялта"}, 7)


def test_make_feature_rejects_bare_name_even_if_the_whitelist_listed_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline import layers as layers_module

    widened = {**LAYER_WHITELIST, "place": frozenset({"id", "name"})}
    monkeypatch.setattr(layers_module, "LAYER_WHITELIST", widened)
    with pytest.raises(WhitelistError):
        make_feature("place", Point(1, 1), {"id": "ua:x", "name": "Ялта"}, 7)


def test_make_feature_rejects_name_on_neutral_landmass() -> None:
    with pytest.raises(WhitelistError):
        make_feature("landmass", Polygon([(0, 0), (1, 0), (1, 1)]), {"class": "neutral", "name:uk": "x"}, 4)


def test_make_feature_rejects_unknown_layer() -> None:
    with pytest.raises(WhitelistError):
        make_feature("roads", Point(1, 1), {}, 4)


def test_make_feature_rejects_forbidden_letter_in_a_value_without_echoing_it() -> None:
    with pytest.raises(WhitelistError) as info:
        make_feature("place", Point(1, 1), {"id": "ua:x", "name:uk": FORBIDDEN}, 7)
    assert FORBIDDEN not in str(info.value)


def test_make_feature_accepts_exactly_the_listed_keys() -> None:
    feature = make_feature("place", Point(1, 2), {"id": "ua:x", "name:uk": "Ялта", "kind": "city", "rank": 3}, 7)
    assert set(feature["properties"]) == {"id", "name:uk", "kind", "rank"}
    assert feature["tippecanoe"] == {"layer": "place", "minzoom": 7, "maxzoom": 12}
    assert feature["geometry"] == {"type": "Point", "coordinates": (1.0, 2.0)}
    assert feature["type"] == "Feature"


def test_make_feature_allows_a_subset_and_an_empty_land() -> None:
    assert make_feature("country", Point(0, 0), {"id": "wd:Q1"}, 4)["properties"] == {"id": "wd:Q1"}
    assert make_feature("land", Point(0, 0), {}, 4)["properties"] == {}


# --- build_layers ---


def test_build_layers_returns_a_geojsonseq_file_per_layer(tmp_path: Path, osm_dir: Path) -> None:
    out = tmp_path / "layers"
    paths = build_layers(_gazetteer(), OUTLINE, BORDER, NEUTRAL_LAND, COUNTRIES, osm_dir, out_dir=out)
    assert set(paths) == set(LAYER_WHITELIST)
    for name, path in paths.items():
        assert path == out / f"{name}.geojsonl"
        assert path.is_file()


def test_every_feature_carries_only_whitelisted_keys_and_a_zoom_range(
    layers: dict[str, list[dict[str, Any]]],
) -> None:
    for name, features in layers.items():
        assert features, name
        for feature in features:
            assert set(feature["properties"]) <= LAYER_WHITELIST[name], name
            assert "name" not in feature["properties"]
            assert feature["tippecanoe"]["layer"] == name
            assert feature["tippecanoe"]["maxzoom"] == 12


def test_land_landmass_and_country_pass_through_at_z4(layers: dict[str, list[dict[str, Any]]]) -> None:
    assert [f["properties"] for f in layers["land"]] == [{}]
    assert _zoom(layers["land"][0]) == 4
    assert [f["properties"] for f in layers["landmass"]] == [{"class": "neutral"}]
    assert _zoom(layers["landmass"][0]) == 4
    assert [f["properties"] for f in layers["country"]] == [
        {"id": "wd:Q36", "name:uk": "Польща"},
        {"id": "wd:Q219"},
    ]
    assert {_zoom(f) for f in layers["country"]} == {4}


def test_place_kinds_and_minzooms(layers: dict[str, list[dict[str, Any]]]) -> None:
    places = _by_id(layers["place"])
    expected = {
        f"ua:{KYIV_CODE}": ("capital", 4),
        f"ua:{SEVASTOPOL_CODE}": ("oblast_centre", 4),
        f"ua:{CENTRE}": ("oblast_centre", 4),
        f"ua:{BIG_CITY}": ("city", 7),
        f"ua:{TWIN_CITY}": ("city", 7),
        f"ua:{SELYSHCHE}": ("selyshche", 9),
        f"ua:{SELO}": ("selo", 11),
    }
    assert {i: (f["properties"]["kind"], _zoom(f)) for i, f in places.items()} == expected
    assert places[f"ua:{CENTRE}"]["geometry"]["coordinates"] == [5.0, 4.0]  # [lon, lat]
    assert places[f"ua:{CENTRE}"]["properties"]["name:uk"] == "Обласний центр"


def test_place_rank_is_tier_then_population_desc_then_id(layers: dict[str, list[dict[str, Any]]]) -> None:
    ordered = sorted(layers["place"], key=lambda f: f["properties"]["rank"])
    assert [f["properties"]["rank"] for f in ordered] == list(range(len(ordered)))
    assert [f["properties"]["id"] for f in ordered] == [
        f"ua:{KYIV_CODE}",  # ярус 1, 2,9 млн
        f"ua:{SEVASTOPOL_CODE}",  # ярус 1, 485 тис.
        f"ua:{CENTRE}",  # ярус 1, 300 тис.
        f"ua:{BIG_CITY}",  # ярус 2, 60 тис., менший id
        f"ua:{TWIN_CITY}",
        f"ua:{SELYSHCHE}",
        f"ua:{SELO}",  # без населення — останнє
    ]


def test_place_skips_settlements_without_a_point(layers: dict[str, list[dict[str, Any]]]) -> None:
    assert f"ua:{SELO_NO_POINT}" not in _by_id(layers["place"])


def test_admin_levels_minzooms_and_clipping(layers: dict[str, list[dict[str, Any]]]) -> None:
    admin = _by_id(layers["admin"])
    assert {i: (f["properties"]["level"], _zoom(f)) for i, f in admin.items()} == {
        f"ua:{OBLAST}": (1, 4),
        f"ua:{RAION}": (2, 7),
        f"ua:{HROMADA_1}": (3, 9),
        f"ua:{HROMADA_2}": (3, 9),
        f"ua:{KYIV_CODE}": (1, 4),  # K — рівень 1; полігон знайдено за katotth
    }
    assert admin[f"ua:{KYIV_CODE}"]["properties"]["name:uk"] == "Київ"
    oblast = shape(admin[f"ua:{OBLAST}"]["geometry"])
    assert oblast.area == pytest.approx(6 * 10)  # box(-2..6) ∩ контур (0..10)
    assert OUTLINE.buffer(1e-9).contains(oblast)


def test_admin_skips_entities_without_a_polygon(layers: dict[str, list[dict[str, Any]]]) -> None:
    assert f"ua:{HROMADA_NO_POLYGON}" not in _by_id(layers["admin"])


def test_admin_line_has_the_state_border_and_no_duplicated_or_offshore_borders(
    layers: dict[str, list[dict[str, Any]]],
) -> None:
    lines = {f["properties"]["level"]: f for f in layers["admin_line"]}
    assert set(lines) == {0, 1, 2, 3}
    assert {level: _zoom(f) for level, f in lines.items()} == {0: 4, 1: 4, 2: 7, 3: 9}
    assert shape(lines[0]["geometry"]).length == pytest.approx(10)
    # громади: спільна межа x = 5 довжиною 10 — один раз; зовнішні межі лежать поза контуром і відпали
    assert shape(lines[3]["geometry"]).length == pytest.approx(10)
    # район: лише його східна межа x = 5 (решта поза контуром)
    assert shape(lines[2]["geometry"]).length == pytest.approx(10)


def test_water_goes_to_water_and_waterway_by_geometry_with_entity_kind(
    layers: dict[str, list[dict[str, Any]]],
) -> None:
    water = _by_id(layers["water"])
    assert set(water) == {"wd:Q10", "osm:w11"}  # безіменна й чужа водойма не потрапили
    assert water["wd:Q10"]["properties"] == {"id": "wd:Q10", "name:uk": "Велике", "kind": "lake"}
    assert {f["properties"]["id"] for f in layers["waterway"]} == {"osm:w13", "wd:Q14", "osm:w16"}  # «Далеко» поза
    assert {f["properties"]["kind"] for f in layers["waterway"]} == {"river"}


def test_water_minzoom_by_area(layers: dict[str, list[dict[str, Any]]]) -> None:
    water = _by_id(layers["water"])
    assert _zoom(water["wd:Q10"]) == 6  # ≈ 44 км² ≥ 20
    assert _zoom(water["osm:w11"]) == 9  # ≈ 11 км²


def test_waterway_minzoom_by_total_entity_length(layers: dict[str, list[dict[str, Any]]]) -> None:
    ways = layers["waterway"]
    zooms = {f["properties"]["id"]: _zoom(f) for f in ways}
    assert zooms["wd:Q14"] == 6  # два шматки по ≈ 66 км — разом ≥ 100 км
    assert zooms["osm:w16"] == 9  # ≈ 55 км
    assert zooms["osm:w13"] == 6  # 5 → 10 після кліпу ≈ 550 км
    assert sum(1 for f in ways if f["properties"]["id"] == "wd:Q14") == 2  # шматки лишаються окремими фічами


def test_waterway_is_clipped_by_the_outline(layers: dict[str, list[dict[str, Any]]]) -> None:
    river = next(f for f in layers["waterway"] if f["properties"]["id"] == "osm:w13")
    assert shape(river["geometry"]).bounds[2] == pytest.approx(10)


def test_peak_layer(layers: dict[str, list[dict[str, Any]]]) -> None:
    peaks = _by_id(layers["peak"])
    assert peaks["wd:Q99"]["properties"] == {"id": "wd:Q99", "name:uk": "Говерла", "ele": 2061}
    assert _zoom(peaks["wd:Q99"]) == 9
    assert peaks["wd:Q99"]["geometry"]["coordinates"] == [8.0, 8.0]
    assert "ele" not in peaks["wd:Q98"]["properties"]


def test_build_layers_is_deterministic(tmp_path: Path, osm_dir: Path) -> None:
    first = build_layers(_gazetteer(), OUTLINE, BORDER, NEUTRAL_LAND, COUNTRIES, osm_dir, out_dir=tmp_path / "a")
    second = build_layers(_gazetteer(), OUTLINE, BORDER, NEUTRAL_LAND, COUNTRIES, osm_dir, out_dir=tmp_path / "b")
    for name in first:
        assert first[name].read_bytes() == second[name].read_bytes(), name
