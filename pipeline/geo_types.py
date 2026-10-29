"""Спільні геометричні типи конвеєра."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TypedDict

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid


class Feature(TypedDict):
    """Просторовий об'єкт: геометрія й плоскі властивості (лише з білого списку шару)."""

    geometry: BaseGeometry
    properties: dict[str, str | int | float]


def _polygons(geometry: BaseGeometry) -> Iterator[Polygon]:
    if isinstance(geometry, Polygon):
        if not geometry.is_empty:
            yield geometry
    elif hasattr(geometry, "geoms"):  # Multi*/GeometryCollection: розгортаємо рекурсивно
        for part in geometry.geoms:
            yield from _polygons(part)


def as_multipolygon(geometry: BaseGeometry) -> MultiPolygon:
    """Валідні лише площинні частини геометрії (точки й лінії від перетинів відкидаються)."""
    return MultiPolygon(list(_polygons(make_valid(geometry))))
