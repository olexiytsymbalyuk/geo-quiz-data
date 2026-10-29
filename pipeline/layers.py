"""Шари тайлів атласу «Україна»: білий список атрибутів, мінзуми, кліп по контуру (спека §5.4).

Кожна фіча проходить `make_feature`: невідомий шар чи ключ поза `LAYER_WHITELIST`, голе `name`
і будь-яке значення із забороненою літерою валять збірку. У тексті помилок — лише імена шарів і ключів,
ніколи значення (назви, id учнів тут немає, але правило єдине).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from pyproj import Geod
from shapely import prepare
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

from pipeline.gazetteer import Entity, GazetteerResult, read_layer, water_entity_id
from pipeline.geo_types import Feature, as_multipolygon
from pipeline.paths import WORK
from pipeline.text import has_forbidden

MIN_LAYER_ZOOM = 4
MAX_LAYER_ZOOM = 12

# Білий список атрибутів за шарами (спека §5.4). Ключа `name` без суфікса немає ніде.
LAYER_WHITELIST: Mapping[str, frozenset[str]] = {
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
_BARE_NAME = "name"

# K-міста з власним статусом області (категорія K КАТОТТГ): Київ і Севастополь.
KYIV_CODE = "UA80000000000093317"
SEVASTOPOL_CODE = "UA85000000000065278"
_K_CODES = frozenset({KYIV_CODE, SEVASTOPOL_CODE})

# Мінзуми — стартова точка, [виміряти] під бюджети й щільність підписів.
_PLACE_MINZOOM = {"capital": 4, "oblast_centre": 4, "city": 7, "selyshche": 9, "selo": 11}
_ADMIN_MINZOOM = {0: 4, 1: 4, 2: 7, 3: 9}
_ADMIN_LEVEL = {"oblast": 1, "raion": 2, "hromada": 3}
PEAK_MINZOOM = 9
LARGE_ZOOM = 6
SMALL_ZOOM = 9
# Стартова точка, [виміряти]: водойма з площею від цього порогу видна з z6, менша — з z9.
WATER_AREA_KM2_LARGE = 20.0
# Стартова точка, [виміряти]: річка із сумарною довжиною всіх шматків від цього порогу видна з z6.
WATERWAY_LENGTH_KM_LARGE = 100.0

_WATER_TYPES = frozenset({"river", "canal", "lake", "reservoir", "liman"})
_GEOD = Geod(ellps="WGS84")
_M_PER_KM = 1000.0
_M2_PER_KM2 = 1_000_000.0


class WhitelistError(ValueError):
    """Фіча порушує білий список атрибутів шару."""


def make_feature(
    layer: str,
    geometry: BaseGeometry,
    properties: Mapping[str, str | int | float],
    minzoom: int,
    maxzoom: int = MAX_LAYER_ZOOM,
) -> dict[str, Any]:
    """GeoJSON-рядок з `tippecanoe`-ключем; перед записом перевіряє білий список."""
    allowed = LAYER_WHITELIST.get(layer)
    if allowed is None:
        raise WhitelistError(f"невідомий шар {layer!r}")
    for key, value in properties.items():
        if key == _BARE_NAME or key not in allowed:
            raise WhitelistError(f"ключ {key!r} поза білим списком шару {layer!r}")
        if isinstance(value, str) and has_forbidden(value):
            raise WhitelistError(f"значення ключа {key!r} шару {layer!r} містить заборонену літеру")
    return {
        "type": "Feature",
        "properties": dict(properties),
        "geometry": mapping(geometry),
        "tippecanoe": {"layer": layer, "minzoom": minzoom, "maxzoom": maxzoom},
    }


# --- кліп по контуру ---


class _Clipper:
    """Кліп по контуру з швидкими шляхами «повністю всередині» / «повністю поза»."""

    def __init__(self, outline: MultiPolygon) -> None:
        self._outline = outline
        prepare(outline)

    def clip(self, geometry: BaseGeometry) -> BaseGeometry:
        if self._outline.contains(geometry):
            return geometry
        if not self._outline.intersects(geometry):
            return Polygon()
        return geometry.intersection(self._outline)


def _lines(geometry: BaseGeometry) -> Iterator[LineString]:
    if isinstance(geometry, LineString):
        if not geometry.is_empty:
            yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _lines(part)


def _area_km2(polygons: MultiPolygon) -> float:
    return sum(abs(_GEOD.geometry_area_perimeter(orient(p, 1.0))[0]) for p in polygons.geoms) / _M2_PER_KM2


def _length_km(lines: Sequence[LineString]) -> float:
    return sum(_GEOD.geometry_length(line) for line in lines) / _M_PER_KM


# --- place ---


def _place_kind(entity: Entity) -> str:
    if entity.katottg == KYIV_CODE:
        return "capital"
    if entity.attributes.get("is_oblast_centre") or entity.katottg == SEVASTOPOL_CODE:
        return "oblast_centre"
    return entity.type


def _population(entity: Entity) -> int:
    return 0 if entity.population is None else int(entity.population["value"])


def _place_features(entities: Iterable[Entity]) -> list[dict[str, Any]]:
    settlements = [
        e for e in entities if e.type in ("city", "selyshche", "selo") and e.lat is not None and e.lon is not None
    ]
    # ранг: менше — важливіше; детермінований порядок (ярус, -населення, id)
    settlements.sort(key=lambda e: (e.tier or 0, -_population(e), e.id))
    features: list[dict[str, Any]] = []
    for rank, entity in enumerate(settlements):
        assert entity.lat is not None and entity.lon is not None
        kind = _place_kind(entity)
        properties: dict[str, str | int | float] = {
            "id": entity.id,
            "name:uk": entity.names["uk"],
            "kind": kind,
            "rank": rank,
        }
        features.append(make_feature("place", Point(entity.lon, entity.lat), properties, _PLACE_MINZOOM[kind]))
    return features


# --- admin ---


def _admin_level(entity: Entity) -> int | None:
    if entity.katottg in _K_CODES:
        return 1
    return _ADMIN_LEVEL.get(entity.type)


def load_admin_polygons(entities: Iterable[Entity], admin_path: Path) -> dict[str, MultiPolygon]:
    """Незакліплені полігони O, K, P, H за `geom_ref` (K-міста — за `katotth`); без полігона — немає ключа."""
    by_ref: dict[str, str] = {}
    by_code: dict[str, str] = {}
    for entity in entities:
        if entity.katottg in _K_CODES:
            by_code[entity.katottg] = entity.id
        elif entity.type in _ADMIN_LEVEL and entity.geom_ref is not None:
            by_ref[entity.geom_ref] = entity.id
    found: dict[str, list[MultiPolygon]] = defaultdict(list)
    for feature in read_layer(admin_path):
        props = feature["properties"]
        if props.get("@type") != "relation":
            continue
        entity_id = by_ref.get(f"osm:r{props['@id']}") or by_code.get(str(props.get("katotth")))
        if entity_id is not None:
            found[entity_id].append(as_multipolygon(shape(feature["geometry"])))
    polygons = {i: unary_union(parts) for i, parts in found.items()}
    return {i: as_multipolygon(g) for i, g in polygons.items() if not g.is_empty}


def _admin_layers(
    entities: Iterable[Entity], polygons: Mapping[str, MultiPolygon], clipper: _Clipper
) -> tuple[list[dict[str, Any]], dict[int, list[MultiPolygon]]]:
    features: list[dict[str, Any]] = []
    unclipped: dict[int, list[MultiPolygon]] = defaultdict(list)
    for entity in sorted(entities, key=lambda e: e.id):
        level = _admin_level(entity)
        polygon = polygons.get(entity.id)
        if level is None or polygon is None:
            continue
        unclipped[level].append(polygon)
        clipped = as_multipolygon(clipper.clip(polygon))
        if clipped.is_empty:
            continue
        properties: dict[str, str | int | float] = {"id": entity.id, "name:uk": entity.names["uk"], "level": level}
        features.append(make_feature("admin", clipped, properties, _ADMIN_MINZOOM[level]))
    return features, unclipped


def _admin_line_features(
    unclipped: Mapping[int, list[MultiPolygon]], border: MultiLineString, clipper: _Clipper
) -> list[dict[str, Any]]:
    """Межі рівнів 1–3: об'єднання меж незакліплених полігонів (спільна межа — один раз), потім по контуру."""
    features: list[dict[str, Any]] = []
    if not border.is_empty:
        features.append(make_feature("admin_line", border, {"level": 0}, _ADMIN_MINZOOM[0]))
    for level in sorted(unclipped):
        union = unary_union([polygon.boundary for polygon in unclipped[level]])
        lines = list(_lines(clipper.clip(union)))
        if lines:
            features.append(make_feature("admin_line", MultiLineString(lines), {"level": level}, _ADMIN_MINZOOM[level]))
    return features


# --- вода ---


def _water_entities(entities: Iterable[Entity]) -> dict[str, Entity]:
    return {e.id: e for e in entities if e.type in _WATER_TYPES}


def _water_pieces(
    water_path: Path, wanted: Mapping[str, Entity], clipper: _Clipper
) -> tuple[list[tuple[Entity, MultiPolygon]], list[tuple[Entity, MultiLineString]]]:
    polygons: list[tuple[Entity, MultiPolygon]] = []
    lines: list[tuple[Entity, MultiLineString]] = []
    for feature in read_layer(water_path):
        entity = wanted.get(water_entity_id(feature["properties"]))
        if entity is None or not feature.get("geometry"):
            continue
        geometry = shape(feature["geometry"])
        if geometry.geom_type in ("Polygon", "MultiPolygon"):
            clipped = as_multipolygon(clipper.clip(as_multipolygon(geometry)))
            if not clipped.is_empty:
                polygons.append((entity, clipped))
        elif geometry.geom_type in ("LineString", "MultiLineString"):
            parts = list(_lines(clipper.clip(geometry)))
            if parts:
                lines.append((entity, MultiLineString(parts)))
    return polygons, lines


def _water_features(polygons: Iterable[tuple[Entity, MultiPolygon]]) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for entity, geometry in polygons:
        zoom = LARGE_ZOOM if _area_km2(geometry) >= WATER_AREA_KM2_LARGE else SMALL_ZOOM
        properties = {"id": entity.id, "name:uk": entity.names["uk"], "kind": entity.type}
        features.append(make_feature("water", geometry, properties, zoom))
    return features


def _waterway_features(lines: Sequence[tuple[Entity, MultiLineString]]) -> list[dict[str, Any]]:
    """Мінзум — за сумарною довжиною всіх шматків сутності, а не окремого шматка."""
    total_km: dict[str, float] = defaultdict(float)
    for entity, geometry in lines:
        total_km[entity.id] += _length_km(list(geometry.geoms))
    features: list[dict[str, Any]] = []
    for entity, geometry in lines:
        zoom = LARGE_ZOOM if total_km[entity.id] >= WATERWAY_LENGTH_KM_LARGE else SMALL_ZOOM
        properties = {"id": entity.id, "name:uk": entity.names["uk"], "kind": entity.type}
        features.append(make_feature("waterway", geometry, properties, zoom))
    return features


# --- вершини, суша, сусіди ---


def _peak_features(entities: Iterable[Entity]) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for entity in entities:
        if entity.type != "peak" or entity.lat is None or entity.lon is None:
            continue
        properties: dict[str, str | int | float] = {"id": entity.id, "name:uk": entity.names["uk"]}
        if "ele" in entity.attributes:
            properties["ele"] = entity.attributes["ele"]
        features.append(make_feature("peak", Point(entity.lon, entity.lat), properties, PEAK_MINZOOM))
    return features


def _passthrough(layer: str, source: Iterable[Feature]) -> list[dict[str, Any]]:
    return [make_feature(layer, f["geometry"], f["properties"], MIN_LAYER_ZOOM) for f in source]


def _check_no_neutral_country(countries: Iterable[Feature], neutral_ids: frozenset[str]) -> None:
    """Нейтральна країна в шарі `country` — порушення правила 2.1; в повідомленні id немає."""
    if any(feature["properties"].get("id") in neutral_ids for feature in countries):
        raise WhitelistError("шар country містить нейтральну країну")


def _write_layer(path: Path, features: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as sink:
        for feature in features:
            sink.write(json.dumps(feature, ensure_ascii=False, separators=(",", ":")) + "\n")


def build_layers(
    gazetteer: GazetteerResult,
    outline: MultiPolygon,
    border: MultiLineString,
    landmass: Sequence[Feature],
    countries: Sequence[Feature],
    osm: Path,
    *,
    neutral_ids: frozenset[str],
    out_dir: Path | None = None,
) -> dict[str, Path]:
    """GeoJSONSeq на шар у `out_dir` (типово `data/work/layers/`); `osm` — тека витягу (`admin`, `water`).

    `neutral_ids` — `wd:<QID>` нейтральних країн NE: другий бар'єр, `country` з таким `id` валить збірку.
    """
    _check_no_neutral_country(countries, neutral_ids)
    target = WORK / "layers" if out_dir is None else out_dir
    target.mkdir(parents=True, exist_ok=True)
    entities = gazetteer.entities
    clipper = _Clipper(outline)

    admin_features, unclipped = _admin_layers(entities, load_admin_polygons(entities, osm / "admin.geojsonl"), clipper)
    water_polygons, water_lines = _water_pieces(osm / "water.geojsonl", _water_entities(entities), clipper)
    layers: dict[str, list[dict[str, Any]]] = {
        "landmass": _passthrough("landmass", landmass),
        "country": _passthrough("country", countries),
        "land": [make_feature("land", outline, {}, MIN_LAYER_ZOOM)],
        "admin": admin_features,
        "admin_line": _admin_line_features(unclipped, border, clipper),
        "place": _place_features(entities),
        "water": _water_features(water_polygons),
        "waterway": _waterway_features(water_lines),
        "peak": _peak_features(entities),
    }
    paths: dict[str, Path] = {}
    for name in LAYER_WHITELIST:
        paths[name] = target / f"{name}.geojsonl"
        _write_layer(paths[name], layers[name])
    return paths
