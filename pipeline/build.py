"""Повна збірка релізу: `python -m pipeline.build --version 2026.11.1` -> `dist/<версія>/`.

Кроки: fetch (з кешу, з перевіркою sha256) -> КАТОТТГ -> витяг OSM -> Wikidata -> газетир -> контур і нейтральна суша
-> шари -> tippecanoe -> штамп `dataset_version` -> `metrics.json` -> `manifest.json`. Ворота не запускаються — це окрема команда.

Збірка пише в `dist/<версія>.partial/` і наприкінці перейменовує теку в `dist/<версія>/`.
Якщо крок падає, тека `.partial` лишається для діагностики.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from shapely.ops import unary_union

from pipeline.extract import extract
from pipeline.fetch import LOCK, SOURCES, fetch_all
from pipeline.gazetteer import GazetteerResult, build_from_cache, build_report, drop_peaks_outside, read_layer
from pipeline.gazetteer_io import write_scope_json, write_sqlite
from pipeline.katottg import KatottgUnit, parse_katottg
from pipeline.layers import KYIV_CODE, SEVASTOPOL_CODE, build_layers, load_admin_polygons
from pipeline.manifest import write_manifest
from pipeline.metrics import StepRecorder, border_length_km, coverage_by_oblast, coverage_by_type
from pipeline.neutral import build_countries, build_landmass, neutral_country_ids
from pipeline.outline import build_outline, load_land_mask, state_border
from pipeline.paths import RAW, ROOT, WORK, dist_dir, validate_version
from pipeline.policy import load_policy
from pipeline.tiles import read_metadata, run_tippecanoe, stamp_metadata, tile_stats
from pipeline.wikidata import WikidataResult
from pipeline.wikidata import fetch as fetch_wikidata

log = logging.getLogger(__name__)

_BBOX_MARGIN_DEG = 0.5
_ADMIN_OBLAST_TYPE = "oblast"
_K_CODES = frozenset({KYIV_CODE, SEVASTOPOL_CODE})
METRICS_NAME = "metrics.json"


class BuildError(RuntimeError):
    """Збірку не можна почати або продовжити (наприклад, реліз уже існує)."""


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _gazetteer_files(gazetteer: GazetteerResult, partial: Path, version: str) -> None:
    write_sqlite(gazetteer, partial / f"gazetteer-{version}.sqlite")
    write_scope_json(gazetteer, partial / f"gazetteer-ukraine-{version}.json")


def _produce(partial: Path, version: str) -> None:
    """Усі кроки конвеєра; артефакти й `metrics.json` лягають у `partial`, потім `manifest.json`."""
    rec = StepRecorder()

    with rec.step("fetch"):
        paths = fetch_all(SOURCES, LOCK, RAW)
    with rec.step("katottg"):
        units: list[KatottgUnit] = parse_katottg(paths["katottg"])
    extract_dir = WORK / "extract"
    extraction = extract(paths["osm_ukraine"], extract_dir)
    rec.add_tool("extract", extraction.run)
    with rec.step("wikidata"):
        wikidata: WikidataResult = fetch_wikidata(RAW)
    with rec.step("gazetteer"):
        gazetteer = build_from_cache(version, units=units, wd=wikidata, extract_dir=extract_dir)

    with rec.step("outline"):
        polygons = load_admin_polygons(gazetteer.entities, extract_dir / "admin.geojsonl")
        land_ids = {e.id for e in gazetteer.entities if e.type == _ADMIN_OBLAST_TYPE or e.katottg in _K_CODES}
        land_polygons = [polygon for entity_id, polygon in polygons.items() if entity_id in land_ids]
        admin_union = unary_union(land_polygons)
        min_x, min_y, max_x, max_y = admin_union.bounds
        bbox = (min_x - _BBOX_MARGIN_DEG, min_y - _BBOX_MARGIN_DEG, max_x + _BBOX_MARGIN_DEG, max_y + _BBOX_MARGIN_DEG)
        outline = build_outline(land_polygons, load_land_mask(paths["land_polygons"], bbox))
        border = state_border(outline, admin_union)
        gazetteer = drop_peaks_outside(gazetteer, outline)  # вершини з буфера витягу, що лежать поза контуром

    ne_shp = paths["ne_admin0_ukr"]
    with rec.step("neutral"):
        policy = load_policy()
        landmass = build_landmass(ne_shp, outline, policy.neutral_countries)
        countries = build_countries(ne_shp, outline, policy)
        neutral_ids = neutral_country_ids(ne_shp, outline, policy.neutral_countries)

    with rec.step("layers"):
        layer_paths = build_layers(gazetteer, outline, border, landmass, countries, extract_dir, neutral_ids=neutral_ids)

    unstamped = WORK / "ukraine-unstamped.pmtiles"
    tippecanoe = run_tippecanoe(layer_paths, unstamped)
    rec.add_tool("tippecanoe", tippecanoe)

    archive = partial / f"ukraine-{version}.pmtiles"
    with rec.step("stamp"):
        stamp_metadata(unstamped, archive, {"dataset_version": version})
    unstamped.unlink()
    if read_metadata(archive)["dataset_version"] != version:
        raise BuildError("штамп dataset_version не потрапив у метадані PMTiles")

    with rec.step("gazetteer_files"):
        _gazetteer_files(gazetteer, partial, version)

    polygon_ids = set(polygons)
    metrics: dict[str, Any] = {
        "dataset_version": version,
        "steps": rec.as_dict(),
        "gazetteer": build_report(gazetteer),
        "coverage_by_type": coverage_by_type(gazetteer.entities, polygon_ids),
        "coverage_by_oblast": coverage_by_oblast(gazetteer.entities, polygon_ids),
        "layers": {
            name: {"features": sum(1 for _ in read_layer(path)), "bytes": path.stat().st_size}
            for name, path in layer_paths.items()
        },
        "artifact_bytes": {p.name: p.stat().st_size for p in sorted(partial.iterdir()) if p.is_file()},
        "tile_stats": tile_stats(archive, outline.bounds),
        "state_border_km": round(border_length_km(border), 1),
        "neutral_country_ids": sorted(neutral_ids),
    }
    _write_json(partial / METRICS_NAME, metrics)
    write_manifest(partial, version, LOCK, wikidata.queried_at)


def build(version: str, *, force: bool = False, dist_root: Path | None = None) -> Path:
    """Збирає реліз у `<dist>/<версія>.partial/`, а за успіху переіменовує в `<dist>/<версія>/`."""
    final = dist_dir(version) if dist_root is None else dist_root / validate_version(version)
    partial = final.with_name(f"{final.name}.partial")
    if final.exists() and not force:
        raise BuildError(f"{final} уже існує; для перезапису додайте --force")
    shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    try:
        _produce(partial, version)
    except BaseException:
        log.error("збірка перервана; тека для діагностики лишена: %s", partial)
        raise
    if final.exists():
        shutil.rmtree(final)
    partial.replace(final)
    log.info("реліз %s готовий: %s", version, final)
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.build", description="Повна збірка релізу датасету.")
    parser.add_argument("--version", required=True, help="версія датасету YYYY.MM.N")
    parser.add_argument("--force", action="store_true", help="перезаписати наявний dist/<версія>/")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        final = build(args.version, force=args.force)
    except (BuildError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(f"{final.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
