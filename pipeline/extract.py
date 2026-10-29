"""Витяг OSM України з білим списком тегів.

`osmium tags-filter` відбирає об'єкти, `osmium export` (GeoJSONSeq) перетворює їх на геометрію,
далі потоково в Python кожна фіча проходить `strip_tags`: з тегів лишається тільки `KEEP_TAGS`.
Голе `name` і `name:*`, крім `name:uk`, не потрапляють у вихідні файли. Властивості `@type`/`@id`
(тип і id OSM-об'єкта) — не теги; osmium додає їх за `--attributes type,id`, і вони зберігаються
(газетиру потрібен `osm_id`). Проміжні файли osmium із сирими тегами видаляються після обробки.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.paths import RAW, WORK
from pipeline.tools import ToolRun, run_tool

KEEP_TAGS = frozenset(
    {"katotth", "name:uk", "place", "admin_level", "boundary", "natural", "waterway", "water", "wikidata", "ele", "population"}
)
OSM_REF_PROPS = ("@type", "@id")  # не теги: тип і id OSM-об'єкта
_RECORD_SEPARATOR = "\x1e"
_RAW_DIR = "_osmium"  # проміжні файли із сирими тегами; видаляється наприкінці

_WATERWAYS = frozenset({"river", "canal"})


def is_admin(feature: Mapping[str, Any]) -> bool:
    """Адмінполігон — лише з relation; closed ways, що tags-filter підтягнув як члени, відкидаються."""
    return (feature.get("properties") or {}).get("@type") == "relation"


def is_place(feature: Mapping[str, Any]) -> bool:
    return "place" in (feature.get("properties") or {})


def is_water(feature: Mapping[str, Any]) -> bool:
    props = feature.get("properties") or {}
    return props.get("natural") == "water" or props.get("waterway") in _WATERWAYS


def is_peak(feature: Mapping[str, Any]) -> bool:
    return (feature.get("properties") or {}).get("natural") == "peak"


# (шар, вирази tags-filter, --geometry-types для export, предикат: що лишити після export)
# `r/katotth` — крім адмінмеж, КАТОТТГ-код мають і мультиполігони контурів населених пунктів
# (type=multipolygon + place=*, без boundary=administrative).
_LAYERS: tuple[tuple[str, tuple[str, ...], str, Callable[[Mapping[str, Any]], bool]], ...] = (
    ("admin", ("r/boundary=administrative", "r/katotth"), "polygon", is_admin),
    ("places", ("n/place",), "point", is_place),
    ("water", ("nwr/natural=water", "w/waterway=river,canal"), "linestring,polygon", is_water),
    ("peaks", ("n/natural=peak",), "point", is_peak),
)


def strip_tags(tags: Mapping[str, str]) -> dict[str, str]:
    """Лише теги з `KEEP_TAGS`; решта (зокрема `name`, `name:ru`, `name:en`) відкидається."""
    return {k: v for k, v in tags.items() if k in KEEP_TAGS}


def clean_feature(feature: Mapping[str, Any]) -> dict[str, Any]:
    """Фіча GeoJSON без заборонених тегів; `@type`/`@id` і геометрія зберігаються."""
    props: Mapping[str, Any] = feature.get("properties") or {}
    cleaned: dict[str, Any] = {k: props[k] for k in OSM_REF_PROPS if k in props}
    cleaned.update(strip_tags({k: v for k, v in props.items() if k not in OSM_REF_PROPS}))
    return {"type": "Feature", "properties": cleaned, "geometry": feature.get("geometry")}


def clean_geojsonseq(
    src: Path, dst: Path, keep: Callable[[Mapping[str, Any]], bool] | None = None
) -> tuple[int, int]:
    """Потоково: GeoJSONSeq osmium → `clean_feature` → `dst` (по фічі на рядок).

    `keep` (за очищеною фічею) відсікає об'єкти, яких не просили, але які tags-filter залишив як члени
    relation. Повертає (кількість записаних фіч, кількість із `katotth`). Запис через `.part` → `replace`.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    total = with_katotth = 0
    try:
        with src.open(encoding="utf-8") as fin, part.open("w", encoding="utf-8") as fout:
            for line in fin:
                line = line.replace(_RECORD_SEPARATOR, "").strip()
                if not line:
                    continue
                cleaned = clean_feature(json.loads(line))
                if keep is not None and not keep(cleaned):
                    continue
                fout.write(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")) + "\n")
                total += 1
                with_katotth += "katotth" in cleaned["properties"]
        part.replace(dst)
    finally:
        part.unlink(missing_ok=True)
    return total, with_katotth


@dataclass(frozen=True)
class ExtractResult:
    admin: Path
    places: Path
    water: Path
    peaks: Path
    run: ToolRun  # підсумок: сума тривалості, максимум пікової RAM
    runs: tuple[tuple[str, ToolRun], ...] = ()  # кожна команда окремо
    counts: Mapping[str, tuple[int, int]] = field(default_factory=dict)  # шар → (фіч, з katotth)


def extract(pbf: Path, out_dir: Path) -> ExtractResult:
    """OSM PBF → `admin/places/water/peaks.geojsonl` в `out_dir` (лише теги з `KEEP_TAGS`)."""
    pbf = pbf.resolve()
    out_dir = out_dir.resolve()
    raw_dir = out_dir / _RAW_DIR
    shutil.rmtree(raw_dir, ignore_errors=True)
    raw_dir.mkdir(parents=True)
    mounts = {pbf.parent: "/data:ro", out_dir: "/out"}
    runs: list[tuple[str, ToolRun]] = []
    counts: dict[str, tuple[int, int]] = {}
    try:
        all_exprs = [e for _, exprs, _, _ in _LAYERS for e in exprs]
        base = f"/out/{_RAW_DIR}/base.osm.pbf"
        runs.append(
            ("tags-filter:base", run_tool(["osmium", "tags-filter", "--overwrite", "-o", base, f"/data/{pbf.name}", *all_exprs], mounts))
        )
        for layer, exprs, geometry_types, keep in _LAYERS:
            filtered = f"/out/{_RAW_DIR}/{layer}.osm.pbf"
            seq = f"/out/{_RAW_DIR}/{layer}.geojsonseq"
            runs.append(
                (f"tags-filter:{layer}", run_tool(["osmium", "tags-filter", "--overwrite", "-o", filtered, base, *exprs], mounts))
            )
            runs.append(
                (
                    f"export:{layer}",
                    run_tool(
                        [
                            "osmium", "export", "--overwrite", "-f", "geojsonseq",
                            "--format-option", "print_record_separator=false",
                            "--attributes", "type,id",
                            f"--geometry-types={geometry_types}",
                            "-o", seq, filtered,
                        ],
                        mounts,
                    ),
                )
            )
            counts[layer] = clean_geojsonseq(raw_dir / f"{layer}.geojsonseq", out_dir / f"{layer}.geojsonl", keep)
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)
    rss = [r.max_rss_mb for _, r in runs if r.max_rss_mb is not None]
    total = ToolRun(seconds=sum(r.seconds for _, r in runs), max_rss_mb=max(rss) if rss else None)
    return ExtractResult(
        admin=out_dir / "admin.geojsonl",
        places=out_dir / "places.geojsonl",
        water=out_dir / "water.geojsonl",
        peaks=out_dir / "peaks.geojsonl",
        run=total,
        runs=tuple(runs),
        counts=counts,
    )


def run_report(result: ExtractResult) -> dict[str, Any]:
    """Дані для `data/work/extract-run.json`."""
    return {
        "total": {"seconds": round(result.run.seconds, 1), "max_rss_mb": result.run.max_rss_mb},
        "commands": {name: {"seconds": round(r.seconds, 1), "max_rss_mb": r.max_rss_mb} for name, r in result.runs},
        "features": {layer: n for layer, (n, _) in result.counts.items()},
        "katotth_features": {layer: k for layer, (_, k) in result.counts.items()},
    }


def main(argv: list[str] | None = None) -> int:
    """`python -m pipeline.extract` — витяг із кешованого PBF (за `sources.lock`, без завантаження)."""
    from pipeline.fetch import LOCK, SOURCES, fetch_all

    if argv:
        sys.stderr.write("використання: python -m pipeline.extract\n")
        return 2
    pbf = fetch_all(SOURCES, LOCK, RAW)["osm_ukraine"]
    result = extract(pbf, WORK / "extract")
    report = run_report(result)
    (WORK / "extract-run.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
