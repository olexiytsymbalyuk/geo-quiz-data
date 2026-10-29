"""Контур України по суші та державний кордон без берегової лінії."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path

import shapefile
from shapely.geometry import LineString, MultiLineString, MultiPolygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, unary_union

from pipeline.fetch import sha256_of
from pipeline.geo_types import as_multipolygon

# 50 м у градусах широти (≈ 111,32 км на градус); допуск збігу межі контуру з межею об'єднаних областей.
BORDER_TOLERANCE_DEG = 50 / 111_320
_LAND_STEM = "land_polygons"
_LAND_EXTENSIONS = ("shp", "shx", "dbf", "cpg")
_SHA_MARKER = ".source-sha256"  # у теці розпакування: sha256 архіву, з якого вона зроблена
_sha_memo: dict[tuple[str, int, int], str] = {}
PROVENANCE_NAME = "PROVENANCE.json"  # є лише в обрізаному архіві (pipeline.land_clip)


def build_outline(oblast_polys: Sequence[BaseGeometry], land_mask: BaseGeometry) -> MultiPolygon:
    """Об'єднання областей ∩ маска суші: контур не виходить у море."""
    union = unary_union([as_multipolygon(polygon) for polygon in oblast_polys])
    valid_mask = as_multipolygon(land_mask)  # зіпсована маска не має ламати перетин
    return as_multipolygon(union.intersection(valid_mask))


def _lines(geometry: BaseGeometry) -> Iterator[LineString]:
    if isinstance(geometry, LineString):
        if not geometry.is_empty:
            yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _lines(part)


def state_border(outline: MultiPolygon, admin_union: BaseGeometry) -> MultiLineString:
    """Межа контуру без берегової лінії.

    Це частина межі контуру, що лежить у 50-метровому буфері межі об'єднання областей ДО кліпу сушею:
    берег, створений лише маскою суші, до неї не потрапляє.
    """
    if outline.is_empty:
        return MultiLineString()
    near_admin_boundary = admin_union.boundary.buffer(BORDER_TOLERANCE_DEG)
    pieces = list(_lines(outline.boundary.intersection(near_admin_boundary)))
    if not pieces:
        return MultiLineString()
    merged = linemerge(pieces)
    return MultiLineString(list(_lines(merged)))


def _archive_sha256(zip_path: Path) -> str:
    """sha256 архіву; у межах процесу запам'ятовується за (шлях, mtime, розмір) -- ворота читають маску багато разів."""
    stat = zip_path.stat()
    key = (str(zip_path.resolve()), stat.st_mtime_ns, stat.st_size)
    if key not in _sha_memo:
        _sha_memo[key] = sha256_of(zip_path)
    return _sha_memo[key]


def extract_land_polygons(zip_path: Path) -> Path:
    """Розпаковує shp/shx/dbf маски суші в кеш поруч з архівом; повертає шлях до .shp.

    Тека кешу відповідає архіву, лише якщо маркер `.source-sha256` дорівнює sha256 архіву; інакше (інший вміст під тим
    самим іменем, обірване розпакування) тека очищається й розпаковується наново. Маркер пишеться останнім.
    """
    cache_dir = zip_path.with_suffix("")
    target = cache_dir / f"{_LAND_STEM}.shp"
    marker = cache_dir / _SHA_MARKER
    digest = _archive_sha256(zip_path)
    if target.exists() and marker.exists() and marker.read_text(encoding="utf-8") == digest:
        return target
    with zipfile.ZipFile(zip_path) as archive:
        members = {
            Path(name).suffix.lstrip("."): name
            for name in archive.namelist()
            if Path(name).stem == _LAND_STEM and Path(name).suffix.lstrip(".") in _LAND_EXTENSIONS
        }
        if "shp" not in members or "shx" not in members or "dbf" not in members:
            raise ValueError(f"{zip_path.name}: немає файлів {_LAND_STEM}.shp/.shx/.dbf")
        cache_dir.mkdir(parents=True, exist_ok=True)
        for stale in [marker, *(cache_dir / f"{_LAND_STEM}.{extension}" for extension in _LAND_EXTENSIONS)]:
            stale.unlink(missing_ok=True)
        partial = cache_dir / f"{_LAND_STEM}.shp.partial"
        for extension, name in members.items():
            # лише базове ім'я члена: шлях з архіву на диск не переноситься
            destination = cache_dir / f"{_LAND_STEM}.{extension}"
            if extension == "shp":
                destination = partial
            with archive.open(name) as source, destination.open("wb") as sink:
                while chunk := source.read(1 << 20):
                    sink.write(chunk)
        partial.rename(target)
    marker.write_text(digest, encoding="utf-8")  # останнім: наявність маркера = розпакування ціле
    return target


def _check_inside_clip(zip_path: Path, bbox: tuple[float, float, float, float]) -> None:
    """Обрізаний архів містить лише суходіл у своєму `clip_bbox`: запит поза ним дав би мовчки неповну маску."""
    with zipfile.ZipFile(zip_path) as archive:
        if PROVENANCE_NAME not in archive.namelist():
            return  # повний архів osmdata: обмежень немає
        west, south, east, north = json.loads(archive.read(PROVENANCE_NAME))["clip_bbox"]
    if bbox[0] < west or bbox[1] < south or bbox[2] > east or bbox[3] > north:
        raise ValueError(f"{zip_path.name}: bbox запиту виходить за межі обрізки {[west, south, east, north]}")


def load_land_mask(zip_path: Path, bbox: tuple[float, float, float, float]) -> BaseGeometry:
    """Маска суші OSM лише в bbox. Великий shp розпаковується один раз у кеш поруч з архівом
    (ZIP-потік не підтримує швидкого довільного доступу), далі pyshp читає записи за bbox-фільтром.
    Обрізаний архів (`PROVENANCE.json`) відмовляє на bbox поза своєю обрізкою."""
    _check_inside_clip(zip_path, bbox)  # без архіву -- FileNotFoundError: ні кеш, ні межі обрізки не перевірити
    shp_path = extract_land_polygons(zip_path)
    with shapefile.Reader(str(shp_path), encoding="utf-8") as reader:
        polygons = [shape(item.__geo_interface__) for item in reader.iterShapes(bbox=bbox) if item is not None]
    return unary_union(polygons) if polygons else MultiPolygon()
