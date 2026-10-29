"""Нейтральна суша і країни-сусіди (правило 2.1: нейтральна суша — без назв і ідентифікаторів)."""

from __future__ import annotations

from pathlib import Path

import pytest
import shapefile
from shapely.geometry import MultiPolygon, box
from shapely.geometry.base import BaseGeometry

from pipeline.neutral import CLIP_MARGIN_DEG, build_countries, build_landmass
from pipeline.policy import Policy

OUTLINE = MultiPolygon([box(0, 0, 10, 10)])
NEUTRAL = frozenset({"RU", "BY"})


def _policy(named: frozenset[str] = frozenset({"PL"})) -> Policy:
    return Policy(
        neutral_countries=NEUTRAL,
        named_neighbours=named,
        exonym_blocklist=(),
        renames_check=(),
        startable_min=10,
        blocked_sentinel="zz-blocked-test-form",
    )


def _write_ne(
    path: Path,
    rows: list[tuple[str, str, str, BaseGeometry]],
) -> Path:
    """Крихітний аналог NE admin-0: (ISO_A2_EH, WIKIDATAID, NAME_UK, геометрія); ADMIN — навмисна приманка."""
    base = path / "ne"
    with shapefile.Writer(str(base), shapeType=shapefile.POLYGON, encoding="utf-8") as writer:
        for field in ("ISO_A2_EH", "WIKIDATAID", "NAME_UK", "ADMIN"):
            writer.field(field, "C", size=60)
        for iso, wikidata, name_uk, geometry in rows:
            writer.record(iso, wikidata, name_uk, f"admin-{iso}")
            writer.shape(geometry.__geo_interface__)
    return base.with_suffix(".shp")


def _rows() -> list[tuple[str, str, str, BaseGeometry]]:
    return [
        ("UA", "Q212", "Україна", box(0, 0, 10, 10)),
        ("RU", "Q159", "Ніщо", box(10, 0, 12, 10)),
        ("BY", "Q184", "Ніщо", box(-2, 10, 8, 12)),
        ("PL", "Q36", "Польща", box(-2, -2, 0, 8)),
        ("BG", "Q219", "Болгарія", box(0, -2, 8, 0)),
        ("DE", "Q183", "Далека", box(50, 50, 60, 60)),
    ]


def test_landmass_is_a_single_feature_with_only_the_neutral_class(tmp_path: Path) -> None:
    features = build_landmass(_write_ne(tmp_path, _rows()), OUTLINE, NEUTRAL)

    assert len(features) == 1  # RU і BY злиті: з тайлів не відрізнити одну від одної
    assert features[0]["properties"] == {"class": "neutral"}
    geometry = features[0]["geometry"]
    assert geometry.intersection(OUTLINE).area < 1e-9
    assert geometry.area == pytest.approx(2 * 10 + 10 * 2)  # RU + BY, не рахуючи нічого зайвого


def test_landmass_carries_no_trace_of_the_source_attributes(tmp_path: Path) -> None:
    (feature,) = build_landmass(_write_ne(tmp_path, _rows()), OUTLINE, NEUTRAL)
    assert set(feature["properties"]) == {"class"}
    assert "Q159" not in repr(feature["properties"])
    assert "admin-RU" not in repr(feature["properties"])


def test_landmass_is_empty_list_when_no_neutral_country_nearby(tmp_path: Path) -> None:
    rows = [row for row in _rows() if row[0] not in NEUTRAL]
    assert build_landmass(_write_ne(tmp_path, rows), OUTLINE, NEUTRAL) == []


def test_landmass_is_clipped_to_the_margin_around_the_outline(tmp_path: Path) -> None:
    far_reaching = [("RU", "Q159", "Ніщо", box(10, 0, 500, 10))]
    (feature,) = build_landmass(_write_ne(tmp_path, far_reaching), OUTLINE, NEUTRAL)
    assert feature["geometry"].bounds[2] == pytest.approx(10 + CLIP_MARGIN_DEG)


def test_landmass_excludes_far_away_neutral_country(tmp_path: Path) -> None:
    rows = [("RU", "Q159", "Ніщо", box(100, 100, 110, 110))]
    assert build_landmass(_write_ne(tmp_path, rows), OUTLINE, NEUTRAL) == []


def test_countries_name_only_the_named_neighbours(tmp_path: Path) -> None:
    features = build_countries(_write_ne(tmp_path, _rows()), OUTLINE, _policy())
    by_id = {f["properties"]["id"]: f["properties"] for f in features}

    assert by_id["wd:Q36"] == {"id": "wd:Q36", "name:uk": "Польща"}
    assert by_id["wd:Q219"] == {"id": "wd:Q219"}  # сусід без підпису: без name:uk
    assert "name:uk" not in by_id["wd:Q219"]


def test_countries_exclude_ukraine_neutral_and_far_countries(tmp_path: Path) -> None:
    features = build_countries(_write_ne(tmp_path, _rows()), OUTLINE, _policy())
    ids = {f["properties"]["id"] for f in features}
    assert ids == {"wd:Q36", "wd:Q219"}  # ні UA, ні RU/BY, ні далека країна


def test_countries_do_not_overlap_the_outline(tmp_path: Path) -> None:
    rows = [("PL", "Q36", "Польща", box(-2, -2, 5, 5))]  # заходить у контур
    (feature,) = build_countries(_write_ne(tmp_path, rows), OUTLINE, _policy())
    assert feature["geometry"].intersection(OUTLINE).area < 1e-9
    assert feature["geometry"].area == pytest.approx(7 * 7 - 5 * 5)


def test_countries_do_not_overlap_the_neutral_land(tmp_path: Path) -> None:
    rows = [
        ("RU", "Q159", "Ніщо", box(10, 0, 12, 10)),
        ("PL", "Q36", "Польща", box(10, 5, 13, 8)),  # накладка на нейтральну сушу
    ]
    (feature,) = build_countries(_write_ne(tmp_path, rows), OUTLINE, _policy())
    assert feature["geometry"].intersection(box(10, 0, 12, 10)).area < 1e-9


def test_countries_reject_named_neighbour_with_empty_name(tmp_path: Path) -> None:
    rows = [("PL", "Q36", "", box(-2, -2, 0, 8))]
    with pytest.raises(ValueError, match="PL"):
        build_countries(_write_ne(tmp_path, rows), OUTLINE, _policy())


def test_countries_reject_named_neighbour_with_forbidden_letter(tmp_path: Path) -> None:
    forbidden = "Пол" + chr(0x044B) + "ща"
    rows = [("PL", "Q36", forbidden, box(-2, -2, 0, 8))]
    with pytest.raises(ValueError, match="PL"):
        build_countries(_write_ne(tmp_path, rows), OUTLINE, _policy())


def test_countries_reject_nearby_country_without_wikidata_id(tmp_path: Path) -> None:
    rows = [("BG", "", "Болгарія", box(0, -2, 8, 0))]
    with pytest.raises(ValueError, match="BG"):
        build_countries(_write_ne(tmp_path, rows), OUTLINE, _policy())


def test_forbidden_letter_error_does_not_echo_the_name(tmp_path: Path) -> None:
    forbidden = "Пол" + chr(0x044B) + "ща"
    rows = [("PL", "Q36", forbidden, box(-2, -2, 0, 8))]
    with pytest.raises(ValueError) as info:
        build_countries(_write_ne(tmp_path, rows), OUTLINE, _policy())
    assert forbidden not in str(info.value)


def test_countries_never_keep_slivers_of_ukraine_itself(tmp_path: Path) -> None:
    # NE-Україна ширша за контур (берег, море): залишок після віднімання не має стати «сусідом»
    rows = [("UA", "Q212", "Україна", box(-1, -1, 11, 11))]
    assert build_countries(_write_ne(tmp_path, rows), OUTLINE, _policy()) == []


def test_landmass_subtracts_the_outline_where_neutral_land_overlaps_it(tmp_path: Path) -> None:
    # сценарій Криму: нейтральний квадрат накладається на контур (x 5..10), поза ним лишається x 10..12
    rows = [("RU", "Q159", "Ніщо", box(5, 0, 12, 10))]
    (feature,) = build_landmass(_write_ne(tmp_path, rows), OUTLINE, NEUTRAL)
    geometry = feature["geometry"]
    assert geometry.intersection(OUTLINE).area < 1e-9
    assert geometry.area == pytest.approx(2 * 10)
