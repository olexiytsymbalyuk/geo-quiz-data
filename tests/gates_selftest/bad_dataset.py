"""Крихітний датасет у `tmp_path` без Docker: «добрий» і «поганий» (по одному порушенню на кожні ворота).

Тайли — `mapbox_vector_tile.encode` + `pmtiles.writer`, газетир — SQLite і JSON, плюс `metrics.json` і маніфест.
Заборонені літери задаються кодовими точками (`chr`), а не гліфами чи escape-ами.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import mapbox_vector_tile
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import write

from pipeline.policy import load_policy
from tests.gates_selftest import tiny_world

VERSION = "2099.01.1"
EXTENT = 4096
NEUTRAL_COUNTRY_IDS = ("wd:Q159", "wd:Q184")
FORBIDDEN_SAMPLE = chr(0x044D)  # одна з чотирьох неукраїнських літер

# Порушення -> id єдиного тесту воріт (файл::тест), що має на ньому впасти.
VIOLATIONS: dict[str, str] = {
    "name_ru_key": "test_no_name_ru_key.py::test_tiles_have_no_name_ru_key",
    "forbidden_letter": "test_no_russian_letters.py::test_tiles_have_no_russian_letters",
    "sentinel_name": "test_exonym_blocklist.py::test_tiles_have_no_blocked_names",
    "named_point_in_landmass": "test_no_neutral_country.py::test_no_named_feature_inside_neutral_landmass",
    "bad_sha256": "test_manifest.py::test_artifacts_match_manifest",
    "sources_mismatch": "test_manifest.py::test_manifest_sources_match_lock",
    "crimea_country_ru": "test_crimea_is_ukraine.py::test_crimean_entities_are_ukrainian",
    "crimea_land_hole": "test_crimea_is_ukraine.py::test_land_layer_covers_crimea",
    "perekop_border_line": "test_crimea_is_ukraine.py::test_no_state_border_through_perekop",
    "katottg_name_differs": "test_occupied_uses_katottg.py::test_names_equal_katottg",
    "old_name_in_tiles": "test_occupied_uses_katottg.py::test_renamed_places_use_new_names",
    "districts_120": "test_counts.py::test_entity_count[raion]",
    "outline_hole": "test_borders_full.py::test_land_outline_is_one_polygon",
    "border_gap": "test_borders_full.py::test_state_border_has_no_gaps",
    "border_off_natural_earth": "test_borders_full.py::test_border_matches_natural_earth",
    "startable_soft_sign": "test_chain_letters.py::test_soft_sign_is_never_startable",
    "chain_letter_outside_pool": "test_chain_letters.py::test_chain_letters_belong_to_effective_pool",
}
RAW_NAME = "raw"
WORK_NAME = "work"
LOCK_NAME = "sources.lock"
NE_VERSION = "5.1.2"
OSM_TIMESTAMP = "2099-01-01T00:00:00Z"

_KYIV = "Київ"
_TILE_Z5 = (5, 18, 11)


def tile_xy(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Координати тайла Web Mercator для точки."""
    n = 2**zoom
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


_TILE_Z10 = (10, *tile_xy(34.0, 45.0, 10))  # тайл у bbox Криму


_TILE_Z8 = (8, *tile_xy(37.0, 51.0, 8))  # тайл z8 лише з нейтральною сушею: предок для правила (b) вибірки
_TILE_PEREKOP = (10, *tile_xy(34.2, 46.05, 10))  # тайл z10 у прямокутнику Перекопу
CRIMEA_HOLE = (1456, 1456, 2640, 2640)  # ≈ 2 × 2 км на широті 45°
BORDER_HOLE = (3000, 3000, 3400, 3400)  # ≈ 0,38 км² на широті 50°
BORDER_GAP = (1500, 2100)  # розрив level=0 ≈ 0,9 км на широті 50°


def _box(x0: int, y0: int, x1: int, y1: int) -> str:
    return f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"


def _ring(x0: int, y0: int, x1: int, y1: int) -> str:
    return f"({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0})"


def _polygon_with_hole(outer: tuple[int, int, int, int], hole: tuple[int, int, int, int] | None) -> str:
    holes = "" if hole is None else f",{_ring(*hole)}"
    return f"POLYGON({_ring(*outer)}{holes})"


def _line(x0: int, y0: int, x1: int, y1: int) -> str:
    return f"LINESTRING({x0} {y0},{x1} {y1})"


def _geo_tiles(violation: str | None) -> dict[tuple[int, int, int], list[dict[str, Any]]]:
    """Крим (z12), межа з сусідом (z12) і Перекоп (z10): шари `land`, `landmass`, `admin_line`."""
    edge = tiny_world.BORDER_EDGE_X
    border_line = _line(edge, 0, edge, EXTENT)
    if violation == "border_gap":
        border_line = f"MULTILINESTRING(({edge} 0,{edge} {BORDER_GAP[0]}),({edge} {BORDER_GAP[1]},{edge} {EXTENT}))"
    border_hole = BORDER_HOLE if violation == "outline_hole" else None
    perekop = [{"geometry": _line(0, 1000, EXTENT, 1000), "properties": {"level": 1}}]
    if violation == "perekop_border_line":
        perekop.append({"geometry": _line(0, 1500, EXTENT, 1500), "properties": {"level": 0}})
    crimea_hole = CRIMEA_HOLE if violation == "crimea_land_hole" else None
    return {
        tiny_world.CRIMEA_TILE: [{"name": "land", "features": [{"geometry": _polygon_with_hole((0, 0, EXTENT, EXTENT), crimea_hole), "properties": {}}]}],
        tiny_world.BORDER_TILE: [
            {"name": "landmass", "features": [{"geometry": _box(0, 0, edge, EXTENT), "properties": {"class": "neutral"}}]},
            {"name": "land", "features": [{"geometry": _polygon_with_hole((edge, 0, EXTENT, EXTENT), border_hole), "properties": {}}]},
            {"name": "admin_line", "features": [{"geometry": border_line, "properties": {"level": 0}}]},
        ],
        _TILE_PEREKOP: [{"name": "admin_line", "features": perekop}],
    }


def _place(fid: str, name: str, x: int, y: int) -> dict[str, Any]:
    return {"geometry": f"POINT({x} {y})", "properties": {"id": fid, "name:uk": name, "kind": "city", "rank": 1}}


def _tile_layers(violation: str | None, sentinel: str) -> list[dict[str, Any]]:
    """Лівій половині тайла належить нейтральна суша; усі іменовані фічі лежать у правій."""
    place_props: dict[str, Any] = {"id": "ua:UA80000000000093317", "name:uk": _KYIV, "kind": "city", "rank": 1}
    place_x = 1000 if violation == "named_point_in_landmass" else 3000
    if violation == "name_ru_key":
        place_props = {**place_props, "name:ru": "x"}
    if violation == "forbidden_letter":
        place_props = {**place_props, "name:uk": f"{_KYIV}{FORBIDDEN_SAMPLE}"}
    if violation == "sentinel_name":
        place_props = {**place_props, "name:uk": sentinel}
    return [
        {"name": "landmass", "features": [{"geometry": _box(0, 0, 2048, EXTENT), "properties": {"class": "neutral"}}]},
        {"name": "country", "features": [{"geometry": _box(2200, 100, 3900, 800), "properties": {"id": "wd:Q36", "name:uk": "Польща"}}]},
        {"name": "place", "features": [{"geometry": f"POINT({place_x} 2000)", "properties": place_props}]},
        {"name": "peak", "features": [{"geometry": "POINT(3200 3000)", "properties": {"id": "osm:n1", "name:uk": "Говерла", "ele": 2061}}]},
    ]


def _encode(layers: list[dict[str, Any]]) -> bytes:
    raw = mapbox_vector_tile.encode(layers, default_options={"y_coord_down": True, "extents": EXTENT})
    return gzip.compress(bytes(raw), mtime=0)


def _write_pmtiles(path: Path, violation: str | None, sentinel: str, version: str) -> None:
    quiet_places = [_place("ua:UA05000000000010357", "Вінниця", 2500, 1500)]
    if violation == "old_name_in_tiles":
        quiet_places.append(_place("ua:UA05000000000010358", load_policy().renames_check[0].old, 2600, 1600))
    quiet = [{"name": "place", "features": quiet_places}]
    land_only = [{"name": "landmass", "features": [{"geometry": _box(0, 0, EXTENT, EXTENT), "properties": {"class": "neutral"}}]}]
    tiles = {_TILE_Z5: _tile_layers(violation, sentinel), _TILE_Z10: quiet, _TILE_Z8: land_only, **_geo_tiles(violation)}
    header = {
        "version": 3,
        "tile_compression": Compression.GZIP,
        "tile_type": TileType.MVT,
        "min_lon_e7": 220_000_000,
        "min_lat_e7": 440_000_000,
        "max_lon_e7": 400_000_000,
        "max_lat_e7": 520_000_000,
        "center_zoom": 4,
        "center_lon_e7": 310_000_000,
        "center_lat_e7": 480_000_000,
    }
    metadata = {"name": "ukraine", "dataset_version": version, "attribution": "тест"}
    with write(path) as writer:
        for zxy in sorted(tiles, key=lambda t: zxy_to_tileid(*t)):
            writer.write_tile(zxy_to_tileid(*zxy), _encode(tiles[zxy]))
        writer.finalize(header, metadata)


def _write_json(path: Path, version: str) -> None:
    entities = [{"id": "ua:UA80000000000093317", "type": "city", "name": _KYIV, "lat": 50.45, "lon": 30.52}]
    payload = {"dataset_version": version, "startable": {}, "entities": entities}
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lock_sources() -> dict[str, Any]:
    """Крихітний аналог `config/sources.lock`: лише те, що читають ворота."""
    return {
        "osm_ukraine": {"files": {"ukraine.osm.pbf": {"sha256": "a" * 64, "osm_timestamp": OSM_TIMESTAMP}}},
        "ne_admin0_ukr": {
            "files": {"ne.shp": {"url": f"https://example.invalid/v{NE_VERSION}/ne.shp", "sha256": "b" * 64}}
        },
    }


def lock_path(dataset: Path) -> Path:
    """Крихітний lock лежить поруч із текою релізу."""
    return dataset.parent / LOCK_NAME


def raw_path(dataset: Path) -> Path:
    """Крихітні еталони (аналог `data/raw`) — поза текою релізу, щоб не потрапити в перелік маніфесту."""
    return dataset.parent / RAW_NAME


def work_path(dataset: Path) -> Path:
    """Куди ворота пишуть свої звіти (аналог `data/work/gates`)."""
    return dataset.parent / WORK_NAME


def build_dataset(root: Path, violation: str | None = None, version: str = VERSION) -> Path:
    """Збирає `root/<версія>/` з п'ятьма файлами; `violation=None` — «добрий» датасет, усі ворота зелені."""
    if violation is not None and violation not in VIOLATIONS:
        raise ValueError(f"невідоме порушення {violation!r}")
    sentinel = load_policy().blocked_sentinel
    out = root / version
    out.mkdir(parents=True)
    _write_pmtiles(out / f"ukraine-{version}.pmtiles", violation, sentinel, version)
    entities, startable = tiny_world.apply_violation(violation, *tiny_world.good_world(version))
    tiny_world.write_gazetteer(out / f"gazetteer-{version}.sqlite", entities, startable, version)
    shift = tiny_world.NE_EDGE_SHIFT if violation == "border_off_natural_earth" else 0
    tiny_world.write_raw(raw_path(out), tiny_world.good_world(version)[0], ne_shift_units=shift)  # еталон КАТОТТГ — добрий
    _write_json(out / f"gazetteer-ukraine-{version}.json", version)
    (out / "metrics.json").write_text(
        json.dumps({"dataset_version": version, "neutral_country_ids": list(NEUTRAL_COUNTRY_IDS)}), encoding="utf-8"
    )
    artifacts = {p.name: {"sha256": _sha256(p), "size": p.stat().st_size} for p in sorted(out.iterdir())}
    if violation == "bad_sha256":
        first = next(iter(artifacts))
        artifacts[first] = {**artifacts[first], "sha256": "0" * 64}
    lock_sources = _lock_sources()
    (root / LOCK_NAME).write_text(json.dumps({"sources": lock_sources}), encoding="utf-8")  # JSON є підмножиною YAML
    sources = json.loads(json.dumps(lock_sources))
    if violation == "sources_mismatch":
        sources["osm_ukraine"]["files"]["ukraine.osm.pbf"]["sha256"] = "c" * 64
    manifest = {
        "dataset_version": version,
        "sources": sources,
        "ne_version": NE_VERSION,
        "osm_timestamp": OSM_TIMESTAMP,
        "artifacts": artifacts,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
