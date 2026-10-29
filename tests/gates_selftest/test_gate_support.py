"""Самотест чистих перевірок воріт: кожна ловить своє порушення й не спрацьовує на чистому вході."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import mapbox_vector_tile
import pytest
from shapely.geometry import box, mapping

from pipeline.policy import load_policy
from tests.gates import gate_support as gs
from tests.gates_selftest.bad_dataset import FORBIDDEN_SAMPLE, build_dataset, tile_xy

# Рядок із шести символів: зворотна скісна риска, `u` і чотири hex-цифри літери U+044D.
ESCAPED_LETTER = "\\" + "u044d"


# --- байти ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("codepoint", gs.FORBIDDEN_CODEPOINTS)
def test_forbidden_offsets_finds_each_of_the_eight_letters(codepoint: int) -> None:
    data = b"abc" + chr(codepoint).encode("utf-8") + b"def"

    assert gs.forbidden_offsets(data) == [3]


def test_forbidden_offsets_finds_json_escapes_in_any_case() -> None:
    assert gs.forbidden_offsets(ESCAPED_LETTER.encode()) == [0]
    assert gs.forbidden_offsets(ESCAPED_LETTER.upper().replace("\\U", "\\u").encode()) == [0]


def test_forbidden_offsets_ignores_clean_ukrainian_text() -> None:
    assert gs.forbidden_offsets("Київ Їжакевичі ґанок".encode("utf-8")) == []


def test_tile_string_fields_skip_geometry_bytes_that_look_like_a_letter() -> None:
    """Випадкові байти геометрії можуть збігтися з D0 AB; шукаємо лише в рядкових полях."""
    layers = [{"name": "peak", "features": [{"geometry": "POINT(2000 2000)", "properties": {"id": "x", "ele": 5}}]}]
    tile = mapbox_vector_tile.encode(layers, default_options={"y_coord_down": True, "extents": 4096})
    poisoned = tile + chr(0x042B).encode("utf-8")  # сміття після повідомлення не є рядковим полем шару

    roles = [(layer, role) for layer, role, _ in gs.tile_string_fields(bytes(tile))]

    assert ("peak", "layer_name") in roles and ("peak", "key") in roles
    assert not any(gs.forbidden_offsets(raw) for _, _, raw in gs.tile_string_fields(bytes(tile)))
    assert gs.FORBIDDEN_BYTES_RE.search(poisoned) is not None  # сирий пошук по всьому тайлу дав би хибний збіг


def test_tile_string_fields_expose_a_letter_in_a_value() -> None:
    layers = [{"name": "place", "features": [{"geometry": "POINT(1 1)", "properties": {"name:uk": "а" + FORBIDDEN_SAMPLE}}]}]
    tile = bytes(mapbox_vector_tile.encode(layers, default_options={"y_coord_down": True, "extents": 4096}))

    hits = [(layer, role) for layer, role, raw in gs.tile_string_fields(tile) if gs.forbidden_offsets(raw)]

    assert hits == [("place", "value")]


def test_sqlite_letter_check_reads_text_cells_not_page_bytes(tmp_path: Path) -> None:
    clean, dirty = tmp_path / "clean.sqlite", tmp_path / "dirty.sqlite"
    for path, text in ((clean, "Київ"), (dirty, "К" + FORBIDDEN_SAMPLE)):
        con = sqlite3.connect(path)
        con.execute("create table entity (id text, names text)")
        con.execute("insert into entity values ('a', ?)", (text,))
        con.commit()
        con.close()

    assert gs.sqlite_letter_violations(clean) == []
    assert gs.sqlite_letter_violations(dirty) == [f"dirty.sqlite: таблиця entity, колонка names, рядок 1"]


def test_json_file_check_catches_escaped_letter(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text('{"name": "x' + ESCAPED_LETTER + '"}', encoding="utf-8")

    assert gs.file_letter_violations(path)


def test_violation_text_never_contains_the_value() -> None:
    feature = gs.TileFeature(5, 1, 2, "place", 0, {"id": "К" + FORBIDDEN_SAMPLE, "name:uk": "x"})

    assert FORBIDDEN_SAMPLE not in feature.where()
    assert feature.where() == "(5, 1, 2, place, #0)"


# --- ключі ---------------------------------------------------------------------------------------------------

def _feature(layer: str, props: dict[str, Any]) -> gs.TileFeature:
    return gs.TileFeature(5, 1, 1, layer, 0, props)


@pytest.mark.parametrize(
    "props",
    [{"id": "a", "name:ru": "x"}, {"id": "a", "name": "x"}, {"id": "a", "name:en": "x"}, {"id": "a", "extra": 1}],
)
def test_tile_key_violations_catch_forbidden_and_unlisted_keys(props: dict[str, Any]) -> None:
    assert gs.tile_key_violations([_feature("place", props)])


def test_tile_key_violations_pass_a_whitelisted_feature_and_flag_unknown_layers() -> None:
    clean = _feature("place", {"id": "a", "name:uk": "x", "kind": "city", "rank": 1})

    assert gs.tile_key_violations([clean]) == []
    assert gs.tile_key_violations([_feature("roads", {})])


def test_gazetteer_key_violations_catch_ru_in_names() -> None:
    def gazetteer(names: dict[str, Any]) -> gs.Gazetteer:
        return gs.Gazetteer(Path("x"), [{"id": "ua:1", "names": json.dumps(names)}], [])

    assert gs.gazetteer_key_violations(gazetteer({"uk": "x", "ru": "y"}))
    assert gs.gazetteer_key_violations(gazetteer({"uk": "x", "xx": "y"}))
    assert gs.gazetteer_key_violations(gazetteer({"uk": "x", "en": None, "local": None})) == []


# --- блок-лист -----------------------------------------------------------------------------------------------

def test_blocklist_catches_the_sentinel_after_normalization() -> None:
    blocked = gs.blocked_keys(load_policy())
    sentinel = load_policy().blocked_sentinel

    assert gs.is_blocked(sentinel, blocked)
    assert gs.is_blocked(sentinel.upper().replace("-", "–"), blocked)
    assert not gs.is_blocked("Київ", blocked)


def test_gazetteer_exonym_check_looks_at_aliases_and_former() -> None:
    sentinel = load_policy().blocked_sentinel
    blocked = gs.blocked_keys(load_policy())
    names = {"uk": "Київ", "uk_aliases": [], "en": None, "en_aliases": [sentinel], "former": [], "local": None}
    row = {"id": "ua:1", "names": json.dumps(names), "facts": "[]"}

    assert gs.gazetteer_exonym_violations(gs.Gazetteer(Path("x"), [row], []), blocked)


# --- вибірка тайлів і геометрія -------------------------------------------------------------------------------

def test_select_tiles_takes_all_low_zoom_border_zone_and_a_fixed_share_of_the_rest() -> None:
    low = {(z, x, 0) for z in range(4, 9) for x in range(5)}
    crimea = tile_xy(34.0, 45.0, 11)
    inland = {(11, 1000 + i, 500) for i in range(2000)}  # далеко від прикордоння
    refs = low | {(11, *crimea)} | inland

    chosen = gs.select_tiles(refs)

    assert low <= chosen
    assert (11, *crimea) in chosen
    assert gs.select_tiles(refs) == chosen  # фіксований seed
    assert 0.005 * len(inland) < len(chosen & inland) < 0.05 * len(inland)


# Відомі прикордонні поселення, що лежали за межами перших прямокутників (north Рівненщини, Волині, Луганщини).
BORDER_POINTS = [
    (26.0, 51.92),
    (24.5, 51.91),
    (38.68, 50.0),
    (38.683, 50.004),
    (34.0, 52.4),
    (31.5, 52.5),
]


@pytest.mark.parametrize("zoom", [10, 12])
@pytest.mark.parametrize(("lon", "lat"), BORDER_POINTS)
def test_border_zone_covers_known_border_points(lon: float, lat: float, zoom: int) -> None:
    x, y = tile_xy(lon, lat, zoom)

    assert gs.in_border_zone(zoom, x, y)


def test_select_tiles_takes_every_descendant_of_a_z8_tile_with_landmass() -> None:
    """Правило (b), похідне від даних: предок на z8 містить нейтральну сушу -> усі нащадки z > 8 у вибірці."""
    parent = (8, 100, 60)
    inside = {(z, 100 * 2 ** (z - 8) + dx, 60 * 2 ** (z - 8) + dy) for z in (9, 10, 12) for dx in (0, 1) for dy in (0, 1)}
    elsewhere = {(10, 700 + i, 400) for i in range(3000)}  # далеко від прикордоння й від предка
    refs = inside | elsewhere | {parent}

    without = gs.select_tiles(refs)
    with_rule = gs.select_tiles(refs, landmass_z8=frozenset({(100, 60)}))

    assert inside <= with_rule
    assert not inside <= without
    assert len(with_rule & elsewhere) == len(without & elsewhere)


def test_landmass_z8_tiles_are_read_from_the_archive(tmp_path: Path) -> None:
    dataset = build_dataset(tmp_path)

    found = gs.TileSet(gs.find_one(dataset, "*.pmtiles")).landmass_z8()

    assert found == frozenset({tile_xy(37.0, 51.0, 8)})  # тайл z5 із сушею не рахується, z10 без суші теж


def test_border_zone_covers_every_listed_region_center() -> None:
    for west, south, east, north in gs.BORDER_BBOXES.values():
        x, y = tile_xy((west + east) / 2, (south + north) / 2, 10)
        assert gs.in_border_zone(10, x, y)


LANDMASS = box(0, 0, 2000, 4096)


@pytest.mark.parametrize(
    ("geometry", "expected"),
    [
        (mapping(box(100, 100, 400, 400)), True),  # полігон углиб суші
        (mapping(box(1999, 100, 2600, 400)), False),  # шов: заходить у сушу менше ніж на допуск
        ({"type": "LineString", "coordinates": [(100, 100), (900, 100)]}, True),
        ({"type": "LineString", "coordinates": [(1999, 100), (1999, 900)]}, False),  # лінія вздовж кордону
        ({"type": "Point", "coordinates": (1000, 1000)}, True),
        ({"type": "Point", "coordinates": (3000, 1000)}, False),
    ],
)
def test_feature_inside_landmass_ignores_the_seam_but_catches_deep_overlap(geometry: dict[str, Any], expected: bool) -> None:
    assert gs.feature_inside_landmass(geometry, LANDMASS) is expected


# --- країни, рядки газетиру, маніфест ----------------------------------------------------------------------------

def test_neutral_row_and_country_layer_checks() -> None:
    rows = [{"id": "ua:1", "country": "UA"}, {"id": "x:2", "country": "BY"}]

    assert len(gs.neutral_row_violations(gs.Gazetteer(Path("x"), rows, []), frozenset({"RU", "BY"}))) == 1
    country = _feature("country", {"id": "wd:Q159"})
    assert gs.neutral_country_layer_violations([country], frozenset({"wd:Q159"}))
    assert not gs.neutral_country_layer_violations([_feature("country", {"id": "wd:Q36"})], frozenset({"wd:Q159"}))


def test_manifest_checks_catch_size_missing_and_foreign_files(tmp_path: Path) -> None:
    dataset = build_dataset(tmp_path)
    manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    assert gs.manifest_file_violations(dataset, manifest) == []

    (dataset / "stray.txt").write_text("x", encoding="utf-8")
    assert any("поза маніфестом" in v for v in gs.manifest_file_violations(dataset, manifest))

    name = next(iter(manifest["artifacts"]))
    manifest["artifacts"][name]["size"] += 1
    assert any("розмір" in v for v in gs.manifest_file_violations(dataset, manifest))

    (dataset / name).unlink()
    assert any("відсутній" in v for v in gs.manifest_file_violations(dataset, manifest))


def test_version_violations_flag_every_mismatch() -> None:
    versions = {"a": "2099.01.1", "b": "2099.01.2", "c": None}

    assert len(gs.version_violations(versions, "2099.01.1")) == 2


def test_tile_letter_check_reports_both_byte_and_decoded_hits(tmp_path: Path) -> None:
    dataset = build_dataset(tmp_path, "forbidden_letter")

    found = gs.tile_letter_violations(gs.TileSet(gs.find_one(dataset, "*.pmtiles")))

    assert any("в байтах" in v for v in found)
    assert any("в атрибутах" in v for v in found)
    assert all(FORBIDDEN_SAMPLE not in v for v in found)


def test_multipoint_is_a_violation_when_any_point_is_inside() -> None:
    mixed = {"type": "MultiPoint", "coordinates": [(1000, 1000), (3000, 1000)]}
    outside = {"type": "MultiPoint", "coordinates": [(2500, 1000), (3000, 1000)]}

    assert gs.feature_inside_landmass(mixed, LANDMASS) is True
    assert gs.feature_inside_landmass(outside, LANDMASS) is False


def test_lock_source_violations_catch_each_field() -> None:
    sources = {
        "osm_ukraine": {"files": {"a.pbf": {"osm_timestamp": "2026-01-01T00:00:00Z"}}},
        "ne_admin0_ukr": {"files": {"a.shp": {"url": "https://x.invalid/v5.1.2/a.shp"}}},
    }
    good = {"sources": sources, "ne_version": "5.1.2", "osm_timestamp": "2026-01-01T00:00:00Z"}

    assert gs.lock_source_violations(good, {"sources": sources}) == []
    assert gs.lock_source_violations({**good, "sources": {}}, {"sources": sources})
    assert gs.lock_source_violations({**good, "ne_version": "5.2.0"}, {"sources": sources})
    assert gs.lock_source_violations({**good, "osm_timestamp": "2026-01-02T00:00:00Z"}, {"sources": sources})
    assert gs.lock_source_violations(good, {})  # порожній lock не може підтвердити маніфест
