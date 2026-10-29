"""Нейтральна суша і країни-сусіди з Natural Earth (українська точка зору).

З атрибутів NE читаються лише `ISO_A2_EH`, `WIKIDATAID`, `NAME_UK`; решта назв (зокрема `ADMIN`) не читаються.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import shapefile
from shapely.geometry import MultiPolygon, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from pipeline.geo_types import Feature, as_multipolygon
from pipeline.policy import Policy
from pipeline.text import has_forbidden

# Запас навколо контуру України, градуси: далі країни в тайли не потрапляють.
CLIP_MARGIN_DEG = 3.0
# Україна в NE — власна країна: її замінює контур з областей, тож у сусідів її немає.
HOME_COUNTRY = "UA"
_FIELDS = ["ISO_A2_EH", "WIKIDATAID", "NAME_UK"]


def _clip_box(outline: MultiPolygon) -> BaseGeometry:
    min_x, min_y, max_x, max_y = outline.bounds
    margin = CLIP_MARGIN_DEG
    return box(min_x - margin, min_y - margin, max_x + margin, max_y + margin)


def _nearby_countries(
    ne_shp: Path, window: BaseGeometry
) -> Iterator[tuple[str, str, str, MultiPolygon]]:
    """(ISO, wikidata, name:uk, геометрія, обрізана до вікна) для країн, що зачіпають вікно."""
    min_x, min_y, max_x, max_y = window.bounds
    with shapefile.Reader(str(ne_shp), encoding="utf-8") as reader:
        for item in reader.iterShapeRecords(fields=_FIELDS, bbox=(min_x, min_y, max_x, max_y)):
            if item.shape is None or item.shape.shapeType == shapefile.NULL:
                continue
            record = item.record.as_dict()
            clipped = as_multipolygon(shape(item.shape.__geo_interface__)).intersection(window)
            polygons = as_multipolygon(clipped)
            if polygons.is_empty:
                continue
            yield (
                str(record["ISO_A2_EH"] or ""),
                str(record["WIKIDATAID"] or ""),
                str(record["NAME_UK"] or ""),
                polygons,
            )


def _neutral_union(ne_shp: Path, window: BaseGeometry, neutral: frozenset[str]) -> BaseGeometry:
    parts = [geometry for iso, _, _, geometry in _nearby_countries(ne_shp, window) if iso in neutral]
    return unary_union(parts) if parts else MultiPolygon()


def build_landmass(ne_shp: Path, outline: MultiPolygon, neutral: frozenset[str]) -> list[Feature]:
    """Нейтральна суша: одна об'єднана геометрія всіх нейтральних країн мінус контур.

    Властивості — рівно `{"class": "neutral"}`: ні id, ні назви, ні ISO; злиття в один об'єкт не дає
    відрізнити з тайлів одну країну від іншої.
    """
    window = _clip_box(outline)
    land = as_multipolygon(_neutral_union(ne_shp, window, neutral).difference(outline))
    if land.is_empty:
        return []
    return [{"geometry": land, "properties": {"class": "neutral"}}]


def _neighbour_properties(iso: str, wikidata: str, name_uk: str, named: frozenset[str]) -> dict[str, str | int | float]:
    if not wikidata:
        raise ValueError(f"NE: у країни {iso or '??'} немає WIKIDATAID")
    properties: dict[str, str | int | float] = {"id": f"wd:{wikidata}"}
    if iso in named:
        if not name_uk or has_forbidden(name_uk):
            # сама назва в повідомлення не потрапляє: лише ISO-код
            raise ValueError(f"NE: у сусіда {iso} порожня або заборонена name:uk")
        properties["name:uk"] = name_uk
    return properties


def build_countries(ne_shp: Path, outline: MultiPolygon, policy: Policy) -> list[Feature]:
    """Країни-сусіди мінус контур України і мінус нейтральна суша. Підписані лише `named_neighbours`."""
    window = _clip_box(outline)
    neutral_land = _neutral_union(ne_shp, window, policy.neutral_countries)
    cut = unary_union([outline, neutral_land])
    features: list[Feature] = []
    for iso, wikidata, name_uk, geometry in _nearby_countries(ne_shp, window):
        if iso == HOME_COUNTRY or iso in policy.neutral_countries:
            continue
        remainder = as_multipolygon(geometry.difference(cut))
        if remainder.is_empty:
            continue
        properties = _neighbour_properties(iso, wikidata, name_uk, policy.named_neighbours)
        features.append({"geometry": remainder, "properties": properties})
    return sorted(features, key=lambda feature: str(feature["properties"]["id"]))
