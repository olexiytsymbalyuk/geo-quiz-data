"""Метрики збірки: кроки, покриття газетира, довжина кордону (Завдання 12)."""

from __future__ import annotations

import pytest
from shapely.geometry import LineString, MultiLineString

from pipeline.gazetteer import Entity
from pipeline.metrics import StepRecorder, border_length_km, coverage_by_oblast, coverage_by_type
from pipeline.tools import ToolRun

OBLAST = "ua:UA01000000000013043"
OTHER_OBLAST = "ua:UA05000000000010357"


def _entity(entity_id: str, type_: str, *, lat: float | None = None, population: int | None = None, parents: list[str] | None = None) -> Entity:
    return Entity(
        id=entity_id,
        type=type_,
        names={"uk": "x"},
        lat=lat,
        lon=None if lat is None else 1.0,
        population=None if population is None else {"value": population, "year": None, "source": "osm"},
        parents=[] if parents is None else parents,
    )


ENTITIES = [
    _entity(OBLAST, "oblast", lat=1.0),
    _entity("ua:r1", "raion", lat=1.0, parents=[OBLAST]),
    _entity("ua:r2", "raion", parents=[OBLAST]),
    _entity("ua:c1", "city", lat=1.0, population=10, parents=["ua:h1", OBLAST]),
    _entity("ua:c2", "city", lat=1.0, parents=[OTHER_OBLAST]),
    _entity("osm:w1", "river"),
]


def test_coverage_by_type_counts_total_point_population_and_polygon_for_admin_units() -> None:
    result = coverage_by_type(ENTITIES, polygon_ids={OBLAST})

    assert result["raion"] == {"total": 2, "with_point": 1, "with_population": 0, "with_polygon": 0}
    assert result["oblast"]["with_polygon"] == 1
    assert result["city"] == {"total": 2, "with_point": 2, "with_population": 1}
    assert result["river"] == {"total": 1, "with_point": 0, "with_population": 0}


def test_coverage_by_oblast_groups_by_topmost_parent_and_skips_orphans() -> None:
    result = coverage_by_oblast(ENTITIES, polygon_ids=set())

    assert set(result) == {OBLAST, OTHER_OBLAST}
    assert result[OBLAST]["city"] == {"total": 1, "with_point": 1, "with_population": 1}
    assert result[OBLAST]["raion"]["total"] == 2
    assert result[OBLAST]["oblast"]["total"] == 1
    assert result[OTHER_OBLAST]["city"]["total"] == 1


def test_border_length_is_geodesic_in_km() -> None:
    one_degree_of_meridian = MultiLineString([LineString([(0.0, 0.0), (0.0, 1.0)])])
    assert border_length_km(one_degree_of_meridian) == pytest.approx(110.57, abs=0.1)


def test_border_length_of_empty_border_is_zero() -> None:
    assert border_length_km(MultiLineString()) == 0.0


def test_step_recorder_records_seconds_and_peak_rss_per_step() -> None:
    recorder = StepRecorder()
    with recorder.step("first"):
        pass
    recorder.add_tool("tool", ToolRun(seconds=1.26, max_rss_mb=100.0))

    steps = recorder.as_dict()
    assert list(steps) == ["first", "tool"]
    assert steps["first"]["seconds"] >= 0
    assert steps["first"]["python_peak_rss_mb"] > 0
    assert steps["tool"] == {"seconds": 1.3, "tool_max_rss_mb": 100.0}


def test_step_recorder_records_a_step_that_raised() -> None:
    recorder = StepRecorder()
    with pytest.raises(RuntimeError), recorder.step("boom"):
        raise RuntimeError("x")
    assert "boom" in recorder.as_dict()
