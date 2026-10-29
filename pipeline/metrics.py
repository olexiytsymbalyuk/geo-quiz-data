"""Метрики збірки: виміри кроків, покриття газетира, довжина державного кордону."""

from __future__ import annotations

import resource
import sys
import time
from collections import defaultdict
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from pyproj import Geod
from shapely.geometry import MultiLineString

from pipeline.gazetteer import Entity
from pipeline.layers import KYIV_CODE, SEVASTOPOL_CODE
from pipeline.tools import ToolRun

_BYTES_PER_MB = 1024 * 1024
_KB_PER_MB = 1024
_M_PER_KM = 1000.0
_ADMIN_TYPES = frozenset({"oblast", "raion", "hromada"})
_K_CITY_CODES = frozenset({KYIV_CODE, SEVASTOPOL_CODE})
_GEOD = Geod(ellps="WGS84")


def peak_rss_mb() -> float:
    """Пік пам'яті цього процесу (накопичувальний); на macOS `ru_maxrss` у байтах, на Linux у КБ."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / _BYTES_PER_MB if sys.platform == "darwin" else raw / _KB_PER_MB


class StepRecorder:
    """Час і пік RAM кроків: Python-кроки міряє сам, Docker-інструменти дають `ToolRun`.

    `python_peak_rss_mb` — пік усього процесу від старту (накопичувальний), а не лише кроку.
    """

    def __init__(self) -> None:
        self._steps: dict[str, dict[str, Any]] = {}

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self._steps[name] = {
                "seconds": round(time.monotonic() - started, 1),
                "python_peak_rss_mb": round(peak_rss_mb()),
            }

    def add_tool(self, name: str, run: ToolRun) -> None:
        self._steps[name] = {"seconds": round(run.seconds, 1), "tool_max_rss_mb": run.max_rss_mb}

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return dict(self._steps)


def _is_admin_unit(entity: Entity) -> bool:
    return entity.type in _ADMIN_TYPES or entity.katottg in _K_CITY_CODES


def _oblast_key(entity: Entity) -> str | None:
    if entity.type == "oblast" or entity.katottg in _K_CITY_CODES:
        return entity.id
    return entity.parents[-1] if entity.parents else None


def _coverage(entities: Iterable[Entity], polygon_ids: Collection[str]) -> dict[str, dict[str, int]]:
    """Покриття за типом: усього / з точкою / з населенням; для адмінодиниць ще й з полігоном."""
    rows: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "with_point": 0, "with_population": 0})
    for entity in entities:
        row = rows[entity.type]
        row["total"] += 1
        row["with_point"] += entity.lat is not None and entity.lon is not None
        row["with_population"] += entity.population is not None
        if _is_admin_unit(entity):
            row["with_polygon"] = row.get("with_polygon", 0) + (entity.id in polygon_ids)
    return dict(sorted(rows.items()))


def coverage_by_type(entities: Iterable[Entity], polygon_ids: Collection[str]) -> dict[str, dict[str, int]]:
    return _coverage(entities, polygon_ids)


def coverage_by_oblast(entities: Iterable[Entity], polygon_ids: Collection[str]) -> dict[str, dict[str, dict[str, int]]]:
    """Та сама розбивка за областю (id області); сутності без області (вода, вершини) пропускаються."""
    groups: dict[str, list[Entity]] = defaultdict(list)
    for entity in entities:
        key = _oblast_key(entity)
        if key is not None:
            groups[key].append(entity)
    return {key: _coverage(group, polygon_ids) for key, group in sorted(groups.items())}


def border_length_km(border: MultiLineString) -> float:
    """Геодезична довжина державного кордону (WGS84) у км."""
    if border.is_empty:
        return 0.0
    return float(_GEOD.geometry_length(border)) / _M_PER_KM
