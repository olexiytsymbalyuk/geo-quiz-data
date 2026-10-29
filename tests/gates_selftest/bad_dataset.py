"""Крихітний датасет у `tmp_path` без Docker: «добрий» і «поганий» (по одному порушенню на кожні ворота).

Тайли — `mapbox_vector_tile.encode` + `pmtiles.writer`, газетир — SQLite і JSON, плюс `metrics.json` і маніфест.
Заборонені літери задаються кодовими точками (`chr`), а не гліфами чи escape-ами.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import mapbox_vector_tile
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import write

from pipeline.policy import load_policy

VERSION = "2099.01.1"
EXTENT = 4096
NEUTRAL_COUNTRY_IDS = ("wd:Q159", "wd:Q184")
FORBIDDEN_SAMPLE = chr(0x044D)  # одна з чотирьох неукраїнських літер

# Порушення -> ворота, які мають на ньому впасти.
VIOLATIONS: dict[str, str] = {
    "name_ru_key": "test_no_name_ru_key",
    "forbidden_letter": "test_no_russian_letters",
    "sentinel_name": "test_exonym_blocklist",
    "named_point_in_landmass": "test_no_neutral_country",
    "bad_sha256": "test_manifest",
}

_KYIV = "Київ"
_TILE_Z5 = (5, 18, 11)


def tile_xy(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    """Координати тайла Web Mercator для точки."""
    n = 2**zoom
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


_TILE_Z10 = (10, *tile_xy(34.0, 45.0, 10))  # тайл у bbox Криму


def _box(x0: int, y0: int, x1: int, y1: int) -> str:
    return f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"


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
    quiet = [{"name": "place", "features": [_place("ua:UA05000000000010357", "Вінниця", 2500, 1500)]}]
    tiles = {_TILE_Z5: _tile_layers(violation, sentinel), _TILE_Z10: quiet}
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


def _names(uk: str, extra: dict[str, Any] | None = None) -> str:
    base = {"uk": uk, "en": None, "uk_aliases": [], "en_aliases": [], "local": None, "former": []}
    return json.dumps({**base, **(extra or {})}, ensure_ascii=False, sort_keys=True)


def _write_sqlite(path: Path, version: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        "create table entity (id text primary key, type text not null, names text not null, country text not null,"
        " facts text not null, attributes text not null, dataset_version text not null);"
        "create table meta (dataset_version text not null, startable_json text not null);"
    )
    rows = [
        ("ua:UA80000000000093317", "city", _names(_KYIV), "UA", "[]", "{}", version),
        ("ua:UA05000000000010357", "city", _names("Вінниця"), "UA", "[]", "{}", version),
    ]
    con.executemany("insert into entity values (?,?,?,?,?,?,?)", rows)
    con.execute("insert into meta values (?, ?)", (version, "{}"))
    con.commit()
    con.close()


def _write_json(path: Path, version: str) -> None:
    entities = [{"id": "ua:UA80000000000093317", "type": "city", "name": _KYIV, "lat": 50.45, "lon": 30.52}]
    payload = {"dataset_version": version, "startable": {}, "entities": entities}
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_dataset(root: Path, violation: str | None = None, version: str = VERSION) -> Path:
    """Збирає `root/<версія>/` з п'ятьма файлами; `violation=None` — «добрий» датасет, усі ворота зелені."""
    if violation is not None and violation not in VIOLATIONS:
        raise ValueError(f"невідоме порушення {violation!r}")
    sentinel = load_policy().blocked_sentinel
    out = root / version
    out.mkdir(parents=True)
    _write_pmtiles(out / f"ukraine-{version}.pmtiles", violation, sentinel, version)
    _write_sqlite(out / f"gazetteer-{version}.sqlite", version)
    _write_json(out / f"gazetteer-ukraine-{version}.json", version)
    (out / "metrics.json").write_text(
        json.dumps({"dataset_version": version, "neutral_country_ids": list(NEUTRAL_COUNTRY_IDS)}), encoding="utf-8"
    )
    artifacts = {p.name: {"sha256": _sha256(p), "size": p.stat().st_size} for p in sorted(out.iterdir())}
    if violation == "bad_sha256":
        first = next(iter(artifacts))
        artifacts[first] = {**artifacts[first], "sha256": "0" * 64}
    manifest = {"dataset_version": version, "artifacts": artifacts}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
