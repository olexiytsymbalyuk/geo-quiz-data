"""Обрізка полігонів суші osmdata до bbox датасету: стабільне джерело замість рухомого архіву на 929 МБ.

`osmdata.openstreetmap.de/download/land-polygons-split-4326.zip` оновлюється щодня й не має датованого знімка,
тож холодний fetch у CI завжди падає на sha256. Рішення власника 60: з кешованого оригіналу один раз виробляємо
невеликий архів лише з полігонами, що перетинають bbox датасету + запас `CLIP_MARGIN_DEG` (той самий, що в
`pipeline.neutral`), і публікуємо його як asset релізу `sources-2026-10`. `sources.lock` тримає sha256 цього asset,
а походження (url і sha256 оригіналу, bbox, запас) — у `derived_from` та в `PROVENANCE.json` усередині архіву.

Вихід відтворюваний байт у байт: записи сортуються за стабільним ключем, поля dbf і координати (подвійна точність,
без округлення -- тож маска збірки лишається тією самою) не змінюються, дата в заголовку dbf зафіксована, у zip
незмінні порядок і метадані членів. Ліцензія -- ODbL 1.0 (похідна база даних OpenStreetMap).

    python -m pipeline.land_clip [--source data/originals/land-polygons-split-4326.zip]
                                 [--out-dir data/release-assets] [--write-lock]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import shapefile
import yaml

from pipeline.fetch import LOCK, SOURCES, ChecksumMismatch, sha256_of
from pipeline.neutral import CLIP_MARGIN_DEG
from pipeline.outline import PROVENANCE_NAME, extract_land_polygons
from pipeline.paths import ROOT

Bbox = tuple[float, float, float, float]

SOURCE_KEY = "land_polygons"
RELEASE_ASSETS = ROOT / "data" / "release-assets"
# Оригінал лежить поза data/raw: ворота шукають у data/raw/land_polygons рівно один архів.
DEFAULT_SOURCE = ROOT / "data" / "originals" / "land-polygons-split-4326.zip"
LICENSE = "ODbL 1.0"
ATTRIBUTION = "© OpenStreetMap contributors"
_STEM = "land_polygons"
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_ZIP_ATTR = 0o644 << 16
_ZIP_CREATE_SYSTEM = 3  # Unix: однаковий результат на macOS і Linux
_DBF_DATE = bytes((100, 1, 1))  # байти 1-3 заголовка dbf: рік від 1900, місяць, день -- 2000-01-01
_UNIX_SYSTEM_FILES = ("cpg", "prj")  # копіюються з оригіналу дослівно, якщо є


@dataclass(frozen=True)
class ClipSpec:
    """Звідки обрізаємо і до чого: усе, що потрапляє в `PROVENANCE.json`."""

    source_url: str
    source_sha256: str
    source_size: int
    source_last_modified: str | None
    dataset_bbox: Bbox
    margin_deg: float = CLIP_MARGIN_DEG

    @property
    def clip_bbox(self) -> Bbox:
        west, south, east, north = self.dataset_bbox
        m = self.margin_deg
        return (west - m, south - m, east + m, north + m)


def derived_from(spec: ClipSpec) -> dict[str, Any]:
    """Запис `derived_from` для `sources.yaml` і `sources.lock`."""
    block: dict[str, Any] = {
        "url": spec.source_url,
        "sha256": spec.source_sha256,
        "size": spec.source_size,
        "dataset_bbox": list(spec.dataset_bbox),
        "clip_margin_deg": spec.margin_deg,
    }
    if spec.source_last_modified is not None:
        block["last_modified"] = spec.source_last_modified
    return block


def spec_from_derived(block: dict[str, Any]) -> ClipSpec:
    west, south, east, north = (float(v) for v in block["dataset_bbox"])
    return ClipSpec(
        source_url=str(block["url"]),
        source_sha256=str(block["sha256"]),
        source_size=int(block["size"]),
        source_last_modified=block.get("last_modified"),
        dataset_bbox=(west, south, east, north),
        margin_deg=float(block["clip_margin_deg"]),
    )


def _overlaps(box: Sequence[float], clip: Bbox) -> bool:
    return not (box[2] < clip[0] or box[0] > clip[2] or box[3] < clip[1] or box[1] > clip[3])


def _sort_key(item: shapefile.ShapeRecord) -> tuple[Any, ...]:
    """Стабільний ключ: bbox фігури, значення полів dbf, повний перелік вершин (на випадок збігу решти)."""
    return (tuple(item.shape.bbox), tuple(item.record), tuple(item.shape.points))


def _patch_dbf_date(dbf: bytes) -> bytes:
    return dbf[:1] + _DBF_DATE + dbf[4:]


def _write_shapefile(items: list[shapefile.ShapeRecord], shape_type: int, fields: list[Any]) -> dict[str, bytes]:
    buffers = {suffix: io.BytesIO() for suffix in ("shp", "shx", "dbf")}
    with shapefile.Writer(shp=buffers["shp"], shx=buffers["shx"], dbf=buffers["dbf"], shapeType=shape_type) as writer:
        for field in fields:
            writer.field(field.name, field.field_type, field.size, field.decimal)
        for item in items:
            writer.record(*item.record)
            writer.shape(item.shape)
    return {
        "shp": buffers["shp"].getvalue(),
        "shx": buffers["shx"].getvalue(),
        "dbf": _patch_dbf_date(buffers["dbf"].getvalue()),
    }


def _source_members(source_zip: Path) -> dict[str, bytes]:
    """`cpg` і `prj` оригіналу (за розширенням, незалежно від теки в архіві)."""
    found: dict[str, bytes] = {}
    with zipfile.ZipFile(source_zip) as archive:
        for name in archive.namelist():
            path = Path(name)
            suffix = path.suffix.lstrip(".")
            if path.stem == _STEM and suffix in _UNIX_SYSTEM_FILES:
                found[suffix] = archive.read(name)
    return found


def _provenance(spec: ClipSpec, polygons: int) -> bytes:
    payload = {
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        **{f"source_{k}": v for k, v in _source_fields(spec).items()},
        "dataset_bbox": list(spec.dataset_bbox),
        "clip_margin_deg": spec.margin_deg,
        "clip_bbox": list(spec.clip_bbox),
        "polygons": polygons,
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _source_fields(spec: ClipSpec) -> dict[str, Any]:
    return {
        "url": spec.source_url,
        "sha256": spec.source_sha256,
        "size": spec.source_size,
        "last_modified": spec.source_last_modified,
    }


def _write_zip(dest: Path, members: list[tuple[str, bytes]]) -> None:
    partial = dest.with_name(dest.name + ".part")
    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in members:
                info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = _ZIP_CREATE_SYSTEM
                info.external_attr = _ZIP_ATTR
                archive.writestr(info, data, compresslevel=9)
        partial.replace(dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def clip_land_polygons(source_zip: Path, dest_zip: Path, spec: ClipSpec) -> int:
    """Пише в `dest_zip` полігони, що перетинають `spec.clip_bbox`; повертає їх кількість.

    Оригінал мусить бути саме тим, що записано в `spec` (sha256), інакше `ChecksumMismatch` і нічого не пишеться.
    """
    actual = sha256_of(source_zip)
    if actual != spec.source_sha256:
        raise ChecksumMismatch(f"{source_zip.name}: sha256 {actual} не збігається із записаним походженням {spec.source_sha256}")
    shp_path = extract_land_polygons(source_zip)
    with shapefile.Reader(str(shp_path), encoding="utf-8") as reader:
        shape_type = reader.shapeType
        fields = [f for f in reader.fields if f.name != "DeletionFlag"]
        items = [item for item in reader.iterShapeRecords(bbox=spec.clip_bbox) if item.shape is not None]
    items = [i for i in items if _overlaps(i.shape.bbox, spec.clip_bbox)]
    if not items:
        raise ValueError(f"{source_zip.name}: у bbox {list(spec.clip_bbox)} немає жодного полігона суші")
    items.sort(key=_sort_key)
    files = _write_shapefile(items, shape_type, fields)
    extras = _source_members(source_zip)
    members = [(f"{_STEM}.{suffix}", files[suffix]) for suffix in ("shp", "shx", "dbf")]
    members += [(f"{_STEM}.{suffix}", extras[suffix]) for suffix in _UNIX_SYSTEM_FILES if suffix in extras]
    members.append((PROVENANCE_NAME, _provenance(spec, len(items))))
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    _write_zip(dest_zip, members)
    return len(items)


def _land_entry(sources: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """(специфікація файлу, `derived_from`) першого файла джерела `land_polygons` у `sources.yaml`."""
    config = yaml.safe_load(sources.read_text(encoding="utf-8"))["sources"][SOURCE_KEY]
    item: dict[str, Any] = config["files"][0]
    if "derived_from" not in item:
        raise ValueError(f"{sources.name}: у {SOURCE_KEY} немає derived_from")
    block: dict[str, Any] = item["derived_from"]
    return item, block


def _write_lock_entry(lock: Path, item: dict[str, Any], produced: Path) -> None:
    """Оновлює в `sources.lock` лише запис land_polygons (формат -- як у `pipeline.fetch --update`)."""
    data = yaml.safe_load(lock.read_text(encoding="utf-8"))
    entry = {
        "url": item["url"],
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sha256": sha256_of(produced),
        "size": produced.stat().st_size,
        "derived_from": item["derived_from"],
    }
    data["sources"][SOURCE_KEY] = {"license": data["sources"][SOURCE_KEY]["license"], "files": {item["name"]: entry}}
    header = "# Згенеровано: python -m pipeline.fetch --update. Не редагувати вручну.\n"
    lock.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.land_clip", description=__doc__.split("\n")[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="кешований оригінал land-polygons-split-4326.zip")
    parser.add_argument("--sources", type=Path, default=SOURCES, help="config/sources.yaml")
    parser.add_argument("--lock", type=Path, default=LOCK, help="config/sources.lock (для --write-lock)")
    parser.add_argument("--out-dir", type=Path, default=RELEASE_ASSETS, help="куди покласти asset для релізу")
    parser.add_argument("--write-lock", action="store_true", help="записати sha256 і розмір asset у sources.lock")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    item, block = _land_entry(args.sources)
    destination = args.out_dir / item["name"]
    count = clip_land_polygons(args.source, destination, spec_from_derived(block))
    if args.write_lock:
        _write_lock_entry(args.lock, item, destination)
    sys.stdout.write(f"{destination}\nполігонів: {count}\nрозмір: {destination.stat().st_size}\nsha256: {sha256_of(destination)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
