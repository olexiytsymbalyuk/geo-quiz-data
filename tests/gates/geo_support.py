"""Геометрія воріт: шари тайлів у метричній проєкції, еталони з `data/raw` (маска суші, NE) і чисті перевірки.

Правило 2.1: у текст порушення ніколи не потрапляє назва чи значення — лише id або зона й числа.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import mapbox_vector_tile
import numpy as np
import numpy.typing as npt
import shapefile
import shapely
from pyproj import Transformer
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from tests.gates.gate_support import Gazetteer, TileKey, TileSet, _fields, safe_id

# Азимутна рівноплоща проєкція з центром в Україні: площі точні, спотворення відстаней < 0,2 % на всій території.
METRIC_CRS = "+proj=laea +lat_0=49 +lon_0=32 +datum=WGS84 +units=m"
_TO_METRIC = Transformer.from_crs(4326, METRIC_CRS, always_xy=True)
_LAYER_FIELD = 3
_NAME_FIELD = 1
_LEN_WIRE = 2

Coords = npt.NDArray[np.float64]
Bbox = tuple[float, float, float, float]


def to_metric(lon: Coords, lat: Coords) -> tuple[Coords, Coords]:
    x, y = _TO_METRIC.transform(lon, lat)
    return np.asarray(x), np.asarray(y)


def project(geometry: BaseGeometry) -> BaseGeometry:
    """Геометрія з градусів (довгота, широта) у метри."""
    return shapely.transform(geometry, lambda c: np.column_stack(to_metric(c[:, 0], c[:, 1])))


def _varint_bytes(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def layer_bytes(tile: bytes, layer: str) -> bytes:
    """Тайл, що містить лише вказаний шар (інші шари не розбираються — це в рази швидше)."""
    wanted = layer.encode("utf-8")
    kept = bytearray()
    for number, wire, payload in _fields(tile):
        if number != _LAYER_FIELD or wire != _LEN_WIRE or not isinstance(payload, bytes):
            continue
        if any(f == _NAME_FIELD and v == wanted for f, _, v in _fields(payload)):
            kept += bytes([(_LAYER_FIELD << 3) | _LEN_WIRE]) + _varint_bytes(len(payload)) + payload
    return bytes(kept)


def tile_to_lonlat(z: int, x: int, y: int, extent: int) -> Callable[[Coords], Coords]:
    n = 2.0**z

    def convert(coords: Coords) -> Coords:
        gx = (x + coords[:, 0] / extent) / n
        gy = (y + coords[:, 1] / extent) / n
        lon = gx * 360.0 - 180.0
        lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * gy))))
        return np.column_stack([lon, lat])

    return convert


def layer_features(
    tileset: TileSet, layer: str, zoom: int, wanted: set[TileKey] | None = None, *, metric: bool = True
) -> Iterator[tuple[TileKey, BaseGeometry, dict[str, Any]]]:
    """(тайл, геометрія, атрибути) фіч шару на зумі; геометрія в метрах (`metric`) або в градусах."""
    for (z, x, y), tile in tileset.raw_tiles(wanted, zoom):
        only = layer_bytes(tile, layer)
        if not only:
            continue
        content = mapbox_vector_tile.decode(only, default_options={"y_coord_down": True})[layer]
        extent = int(content.get("extent", 4096))
        to_lonlat = tile_to_lonlat(z, x, y, extent)
        for feature in content["features"]:
            geometry = shapely.transform(shape(feature["geometry"]), to_lonlat)
            yield (z, x, y), (project(geometry) if metric else geometry), feature["properties"]


def tiles_covering(zoom: int, bbox: Bbox) -> set[TileKey]:
    """Усі координати тайлів зуму, що перетинають bbox (мін. довгота, мін. широта, макс. довгота, макс. широта)."""
    n = 2**zoom

    def column(lon: float) -> int:
        return min(n - 1, max(0, int((lon + 180.0) / 360.0 * n)))

    def row(lat: float) -> int:
        return min(n - 1, max(0, int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)))

    west, south, east, north = bbox
    return {(zoom, x, y) for x in range(column(west), column(east) + 1) for y in range(row(north), row(south) + 1)}


def layer_union(
    tileset: TileSet, layer: str, zoom: int, wanted: set[TileKey] | None = None, *, level: int | None = None
) -> BaseGeometry:
    """Об'єднання геометрій шару на зумі (метри); `level` лишає лише `admin_line` / `admin` із цим рівнем."""
    parts = [
        geometry
        for _, geometry, props in layer_features(tileset, layer, zoom, wanted)
        if level is None or props.get("level") == level
    ]
    return unary_union(parts) if parts else shapely.GeometryCollection()


# --- еталони data/raw: маска суші OSM, Natural Earth -----------------------------------------------------------

NE_ISO_FIELD = "ISO_A2_EH"  # з атрибутів NE читається лише він
NE_HOME_ISO = "UA"
NE_WINDOW: Bbox = (20.0, 43.0, 42.0, 55.0)  # довкола України; далі NE не потрібен


def ne_countries(ne_shp: Path, bbox: Bbox = NE_WINDOW) -> tuple[BaseGeometry, BaseGeometry]:
    """(Україна, усі інші країни) з NE у метрах; полігони обрізані до `bbox` (градуси)."""
    window = box(*bbox)
    home: list[BaseGeometry] = []
    others: list[BaseGeometry] = []
    with shapefile.Reader(str(ne_shp), encoding="utf-8") as reader:
        for item in reader.iterShapeRecords(fields=[NE_ISO_FIELD], bbox=bbox):
            if item.shape is None or item.shape.shapeType == shapefile.NULL:
                continue
            clipped = shape(item.shape.__geo_interface__).intersection(window)
            if not clipped.is_empty:
                (home if item.record[NE_ISO_FIELD] == NE_HOME_ISO else others).append(clipped)
    return _metric_union(home), _metric_union(others)


def _metric_union(parts: list[BaseGeometry]) -> BaseGeometry:
    return project(unary_union(parts)) if parts else shapely.GeometryCollection()


def land_mask_metric(zip_path: Path, bbox: Bbox) -> BaseGeometry:
    """Маска суші OSM у bbox (градуси), обрізана по bbox, у метрах."""
    from pipeline.outline import load_land_mask  # імпорт тут: тягне pyshp і розпаковку кешу лише для воріт, що це читають

    return _metric_union([load_land_mask(zip_path, bbox).intersection(box(*bbox))])


# --- Крим: шар `land` покриває маску суші -----------------------------------------------------------------------

CRIMEA_BBOX: Bbox = (32.4, 44.3, 36.7, 46.3)
FINEST_ZOOM = 12
# Опорні точки, що мають лежати в перевірюваній зоні: острів Тузла, Перекоп, Арабатська стрілка (звужена коса,
# 34.943 E / 45.8 N: точка 35.2 / 45.6 з рецензії лежить у Сиваші за 5,7 км від суші маски).
CRIMEA_ANCHORS: tuple[tuple[float, float], ...] = ((36.547, 45.271), (33.78, 46.15), (34.943, 45.8))
ANCHOR_TOLERANCE_M = 250.0
COAST_BUFFER_M = 50.0
MAX_UNCOVERED_KM2 = 0.1
_M2_PER_KM2 = 1_000_000.0


def _components(geometry: BaseGeometry) -> list[BaseGeometry]:
    return [g for g in getattr(geometry, "geoms", [geometry]) if not g.is_empty]


def ne_union(ne_shp: Path, bbox: Bbox, isos: Iterable[str]) -> BaseGeometry:
    """Об'єднання NE-полігонів країн за `ISO_A2_EH` у bbox (метри)."""
    wanted = frozenset(isos)
    window = box(*bbox)
    parts: list[BaseGeometry] = []
    with shapefile.Reader(str(ne_shp), encoding="utf-8") as reader:
        for item in reader.iterShapeRecords(fields=[NE_ISO_FIELD], bbox=bbox):
            if item.shape is not None and item.shape.shapeType != shapefile.NULL and item.record[NE_ISO_FIELD] in wanted:
                parts.append(shape(item.shape.__geo_interface__).intersection(window))
    return _metric_union([p for p in parts if not p.is_empty])


def checked_zone(mask: BaseGeometry, foreign: BaseGeometry) -> BaseGeometry:
    """Маска суші без зв'язних компонент, що перетинають NE-полігон нейтральної країни (Тамань і коса Чушка).

    Острови й усе інше лишаються в перевірці незалежно від того, кому їх віддає NE.
    """
    kept = [part for part in _components(mask) if not part.intersects(foreign)]
    return unary_union(kept) if kept else shapely.GeometryCollection()


def land_gaps(land: BaseGeometry, zone: BaseGeometry) -> tuple[float, float]:
    """(непокрита площа, км²; найбільша зв'язна непокрита компонента після внутрішнього берегового буфера, км²).

    Непокрите = перевірювана зона маски суші мінус шар `land`. Перший вимір — без жодних поблажок; у другому береги
    стискаються на `COAST_BUFFER_M` і назад: спрощення берега в тайлах лишає тисячі субметрових шматків уздовж
    узбережжя, а реальна «дірка» цим не зникає.
    """
    uncovered = zone.difference(land)
    total_km2 = uncovered.area / _M2_PER_KM2
    core = uncovered.buffer(-COAST_BUFFER_M)
    largest_m2 = max((part.buffer(COAST_BUFFER_M).area for part in _components(core)), default=0.0)
    return total_km2, largest_m2 / _M2_PER_KM2


def anchors_outside(zone: BaseGeometry) -> int:
    """Скільки опорних точок (`CRIMEA_ANCHORS`) лежить далі за `ANCHOR_TOLERANCE_M` від перевірюваної зони."""
    lon = np.array([p[0] for p in CRIMEA_ANCHORS])
    lat = np.array([p[1] for p in CRIMEA_ANCHORS])
    x, y = to_metric(lon, lat)
    return int(sum(zone.distance(shapely.Point(px, py)) > ANCHOR_TOLERANCE_M for px, py in zip(x, y, strict=True)))


def crimea_land_gaps(
    tileset: TileSet, zip_path: Path, ne_shp: Path, neutral_isos: Iterable[str]
) -> tuple[float, float, int, int]:
    """(непокрито км², найбільша компонента км², тайлів `land`, опорних точок поза зоною)."""
    wanted = tiles_covering(FINEST_ZOOM, CRIMEA_BBOX)
    parts = [geometry for _, geometry, _ in layer_features(tileset, "land", FINEST_ZOOM, wanted)]
    land = unary_union(parts) if parts else shapely.GeometryCollection()
    mask = land_mask_metric(zip_path, CRIMEA_BBOX)
    assert not mask.is_empty, "маска суші в bbox Криму порожня — перевірка була б порожньою"
    zone = checked_zone(mask, ne_union(ne_shp, CRIMEA_BBOX, neutral_isos))
    total, largest = land_gaps(land, zone)
    return total, largest, len(parts), anchors_outside(zone)


# --- Перекоп ---------------------------------------------------------------------------------------------------

PEREKOP_BBOX: Bbox = (33.5, 45.9, 34.8, 46.2)
ADMIN_LINE_LAYER = "admin_line"
STATE_BORDER_LEVEL = 0


def perekop_lines(tileset: TileSet, zooms: Iterable[int]) -> tuple[int, int]:
    """(лінії `admin_line` усіх рівнів у прямокутнику Перекопу, з них рівня 0) на всіх вказаних зумах."""
    rectangle = box(*PEREKOP_BBOX)
    seen = state = 0
    for zoom in zooms:
        for _, line, props in layer_features(tileset, ADMIN_LINE_LAYER, zoom, tiles_covering(zoom, PEREKOP_BBOX), metric=False):
            if line.intersects(rectangle):
                seen += 1
                state += props.get("level") == STATE_BORDER_LEVEL
    return seen, state


# --- державний кордон ------------------------------------------------------------------------------------------

NEIGHBOUR_TOUCH_M = 100.0  # «торкається сусіда»: у межах стику шарів після спрощення тайлів
COVER_TOLERANCE_M = 100.0  # допуск покриття меж лінією level=0
HOLE_MAX_KM2 = 0.001  # дірки основного полігона менші за це — артефакт квантування тайлів (виміряно: 2·10⁻⁵ км²)
ISLAND_MAX_KM2 = 500.0  # «острів» — частина контуру, що не торкається сусідів; Крим (≈ 27 000 км²) островом не є
MIN_GAP_M = 25.0  # коротші шматки — числовий пил
LAND_SHARE_MIN = 0.5  # частка сусідньої сторони, що має бути сушею OSM, щоб розрив вважався розривом кордону
SAMPLE_STEP_M = 250.0
NE_LAND_BORDER_MARGIN_M = 1000.0
# Пороги відхилення кордону OSM від NE `_ukr` -- затверджені власником 2026-10-29 (єдиний поріг 2 км не відповідав розподілу).
# Виміряно на dist/2026.11.1 з кроком 250 м: p50 1,083 км, p95 2,976 км, max 6,193 км. Кордон OSM деталізований,
# NE 1:10m -- ні, тож «хвіст» розподілу очікуваний; три пороги ловлять і системний зсув (p50), і широкі
# розбіжності (p95), і одиночний викид (max). Пороги нестрогі (<=).
DEVIATION_P50_KM = 1.5
DEVIATION_P95_KM = 3.5
DEVIATION_MAX_KM = 7.0
_M_PER_KM = 1000.0
# Допуск для точок газетира проти нейтральної суші NE. Поселення КАТОТТГ біля кордону через генералізацію NE лягають
# «через лінію»; виміряно на dist/2026.11.1: 112 точок у нейтральній суші NE, найглибша -- 4,5 км. Власний запас ~1 км,
# а не загальний поріг кордону DEVIATION_MAX_KM (7 км), щоб справжня точка в глибині сусіда не проходила.
NEUTRAL_POINT_TOLERANCE_KM = 5.5


def neighbour_union(tileset: TileSet, zoom: int) -> BaseGeometry:
    """Нейтральна суша й країни-сусіди шарів `landmass` і `country` на зумі."""
    return unary_union([layer_union(tileset, "landmass", zoom), layer_union(tileset, "country", zoom)])


def outline_violations(land: BaseGeometry, neighbours: BaseGeometry) -> tuple[list[str], int]:
    """(порушення, кількість частин контуру): один основний полігон без дірок, решта — лише невеликі острови.

    Основний полігон — єдина частина `land`, що торкається суші сусідів. Інша частина, що торкається сусіда,
    означає розрив контуру; велика частина без сусідів (відрізаний Крим) — теж розрив.
    """
    parts = _components(land)
    zone = neighbours.buffer(NEIGHBOUR_TOUCH_M)
    shapely.prepare(zone)
    touching = [p for p in parts if zone.intersects(p)]
    found: list[str] = []
    if len(touching) != 1:
        found.append(f"частин контуру, що торкаються сусідів: {len(touching)}, очікується одна")
    for index, part in enumerate(parts, start=1):
        if part in touching:
            holes = [h for h in (shapely.Polygon(ring) for ring in getattr(part, "interiors", [])) if h.area / _M2_PER_KM2 > HOLE_MAX_KM2]
            found += [f"основний полігон: дірка {h.area / _M2_PER_KM2:.4f} км²" for h in holes]
        elif part.area / _M2_PER_KM2 > ISLAND_MAX_KM2:
            found.append(f"частина №{index} без сусідів завбільшки {part.area / _M2_PER_KM2:.0f} км² — не острів")
    return found, len(parts)


def lonlat_bounds(geometry: BaseGeometry, margin_m: float) -> Bbox:
    """Обмежувальний прямокутник метричної геометрії в градусах із запасом."""
    west, south, east, north = geometry.buffer(margin_m).bounds
    inverse = Transformer.from_crs(METRIC_CRS, 4326, always_xy=True)
    lon0, lat0 = inverse.transform(west, south)
    lon1, lat1 = inverse.transform(east, north)
    return float(min(lon0, lon1)), float(min(lat0, lat1)), float(max(lon0, lon1)), float(max(lat0, lat1))


def border_gaps(
    land: BaseGeometry, neighbours: BaseGeometry, border: BaseGeometry, mask_for: Callable[[Bbox], BaseGeometry]
) -> list[str]:
    """Ділянки межі `land`, що лежать ближче 100 м до сусідів, але не покриті `level=0` у межах 100 м.

    Сусідня сторона мусить бути сушею OSM: NE грубіший за OSM, і біля берега полігон сусіда лягає на море.
    Розрив без суші поруч — це берег, а не кордон. Маска читається лише для знайдених розривів.
    """
    near = land.boundary.intersection(neighbours.buffer(NEIGHBOUR_TOUCH_M))
    uncovered = near.difference(border.buffer(COVER_TOLERANCE_M))
    found: list[str] = []
    for index, piece in enumerate(_components(uncovered), start=1):
        if piece.length <= MIN_GAP_M:
            continue
        side = piece.buffer(NEIGHBOUR_TOUCH_M).intersection(neighbours)
        if side.is_empty:
            continue
        mask = mask_for(lonlat_bounds(piece, NEIGHBOUR_TOUCH_M * 2))
        assert not mask.is_empty, "маска суші порожня в зоні розриву — розрив не відрізнити від берега"
        on_land = side.intersection(mask).area
        if on_land / side.area >= LAND_SHARE_MIN:
            found.append(f"ділянка кордону №{index}: розрив level=0 завдовжки {piece.length:.0f} м")
    return found


def _points(geometry: BaseGeometry) -> shapely.Geometry:
    """Точки вздовж ліній із фіксованим кроком `SAMPLE_STEP_M` (від початку кожної лінії та її кінець): вага — довжина."""
    lines = [part for part in shapely.get_parts(geometry) if part.geom_type in ("LineString", "LinearRing") and part.length > 0]
    if not lines:
        return shapely.points(np.empty((0, 2)))
    chunks = []
    for line in lines:
        distances = np.append(np.arange(0.0, line.length, SAMPLE_STEP_M), line.length)
        chunks.append(shapely.line_interpolate_point(line, distances))
    return np.concatenate(chunks)


def _stats_km(distances: npt.NDArray[np.float64]) -> dict[str, float]:
    if distances.size == 0:
        return {"p50": 0.0, "p95": 0.0, "max": 0.0, "points": 0}
    p50, p95 = np.percentile(distances, [50, 95])
    return {
        "p50": round(float(p50) / _M_PER_KM, 3),
        "p95": round(float(p95) / _M_PER_KM, 3),
        "max": round(float(distances.max()) / _M_PER_KM, 3),
        "points": int(distances.size),
    }


def deviation_violations(overall: Mapping[str, float]) -> list[str]:
    """Порушені затверджені пороги: по одному рядку на кожну з p50, p95, max (текст починається з назви порогу)."""
    limits = (("p50", DEVIATION_P50_KM), ("p95", DEVIATION_P95_KM), ("max", DEVIATION_MAX_KM))
    return [f"{name} {overall[name]} км > {limit} км" for name, limit in limits if overall[name] > limit]


def border_deviation(
    border: BaseGeometry, neighbours: BaseGeometry, ne_home: BaseGeometry, ne_neighbours: BaseGeometry
) -> dict[str, Any]:
    """Відхилення `level=0` від NE по сухопутних ділянках, обидва напрямки (двосторонній Гаусдорф), у км.

    Напрямок «тайли → NE»: точки `level=0` біля сухопутних сусідів тайлів (ближче 100 м) до межі NE-України.
    Напрямок «NE → тайли»: точки межі NE-України ближче `NE_LAND_BORDER_MARGIN_M` до сусідів у NE до `level=0`.
    Відхилення від брифу: 1 км замість 5 км — на 5 км у вибірку потрапляє берег Керченської протоки (4,5 км до
    Росії), який не є сухопутним кордоном і дає хибні 234 км.
    """
    ne_boundary = ne_home.boundary
    tiles_land = border.intersection(neighbours.buffer(NEIGHBOUR_TOUCH_M))
    ne_land = ne_boundary.intersection(ne_neighbours.buffer(NE_LAND_BORDER_MARGIN_M))
    to_ne = shapely.distance(_points(tiles_land), ne_boundary)
    to_tiles = shapely.distance(_points(ne_land), border)
    return {
        "thresholds_km": {"p50": DEVIATION_P50_KM, "p95": DEVIATION_P95_KM, "max": DEVIATION_MAX_KM},
        "level0_to_ne": _stats_km(to_ne),
        "ne_to_level0": _stats_km(to_tiles),
        "overall": _stats_km(np.concatenate([to_ne, to_tiles])),
        "ne_land_border_margin_m": NE_LAND_BORDER_MARGIN_M,
        "zoom": FINEST_ZOOM,
    }


# --- точки газетира проти нейтральної суші ---------------------------------------------------------------------

def neutral_point_violations(gazetteer: Gazetteer, neutral: BaseGeometry, tolerance_m: float) -> tuple[list[str], int]:
    """(порушення, кількість перевірених точок): сутності з координатами глибше за `tolerance_m` у нейтральній суші.

    `neutral` -- NE-полігони нейтральних країн у метрах. Сутності без lat/lon не перевіряються й не рахуються.
    """
    rows = [r for r in gazetteer.entities if r["lat"] is not None and r["lon"] is not None]
    if not rows:
        return [], 0
    x, y = to_metric(np.array([r["lon"] for r in rows], dtype=np.float64), np.array([r["lat"] for r in rows], dtype=np.float64))
    interior = neutral.buffer(-tolerance_m)
    shapely.prepare(interior)
    found = [
        f"sqlite entity {safe_id(row['id'], index)}: точка в нейтральній суші"
        for index, (row, inside) in enumerate(zip(rows, shapely.contains_xy(interior, x, y), strict=True))
        if inside
    ]
    return found, len(rows)
