"""Самотест чистих перевірок воріт задачі 14: кожна ловить своє порушення й не спрацьовує на чистому вході."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from shapely.geometry import LineString, Polygon, box

from pipeline.policy import Rename
from tests.gates import gazetteer_support as gz
from tests.gates import geo_support as geo
from tests.gates.gate_support import Gazetteer, TileFeature
from tests.gates_selftest.bad_dataset import tile_xy

KM = 1000.0
ARK_ID = "ua:UA01000000000013043"


def row(
    entity_id: str, kind: str, name: str, *, code: str = "", parents: list[str] | None = None, country: str = "UA",
    tier: int | None = None, chain: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    return {
        "id": entity_id, "type": kind, "katottg": code or None, "country": country, "chain_letter": chain,
        "names": json.dumps({"uk": name}, ensure_ascii=False), "parents": json.dumps(parents or []),
        "tiers": json.dumps({} if tier is None else {"ukraine": tier}),
    }  # fmt: skip


def gazetteer(rows: list[dict[str, Any]], startable: dict[str, list[str]] | None = None) -> Gazetteer:
    meta = [{"startable_json": json.dumps(startable or {})}]
    return Gazetteer(Path("x.sqlite"), rows, meta)


def crimea_rows() -> list[dict[str, Any]]:
    rows = [row(ARK_ID, "oblast", gz.ARK_NAME, code="UA01000000000013043")]
    for number, name in enumerate(gz.CRIMEA_CITY_NAMES):
        if name == gz.SEVASTOPOL_NAME:
            rows.append(row("ua:UA85000000000065278", "city", name, code="UA85000000000065278"))
        else:
            rows.append(row(f"ua:UA010{number}", "city", name, code=f"UA01{number:015d}", parents=["ua:h", ARK_ID]))
    return rows


# --- Крим -----------------------------------------------------------------------------------------------------------

def test_crimea_check_accepts_seven_cities_and_the_k_unit_of_sevastopol() -> None:
    assert gz.crimea_violations(gazetteer(crimea_rows())) == []


def test_crimea_check_flags_a_city_whose_parents_end_elsewhere() -> None:
    rows = crimea_rows()
    rows[1] = {**rows[1], "parents": json.dumps(["ua:h", "ua:UA05000000000000001"])}

    assert len(gz.crimea_violations(gazetteer(rows))) == 1


def test_crimea_check_flags_a_missing_city_and_a_foreign_country() -> None:
    rows = [r for r in crimea_rows() if gz.uk_name(r) != "Керч"]
    rows[0] = {**rows[0], "country": "RU"}

    assert len(gz.crimea_violations(gazetteer(rows))) == 2


# --- КАТОТТГ і перейменування ----------------------------------------------------------------------------------------

def feature(layer: str, **props: Any) -> TileFeature:
    return TileFeature(10, 1, 2, layer, 0, props)


RENAME = Rename("Староград", "Новоград", "2016-02-04", "тест")


def test_rename_check_excuses_a_coded_namesake_of_another_type_and_its_tile_label() -> None:
    rows = [row("ua:a", "city", "Новоград", code="UA01000000000000001"), row("ua:b", "selo", "Староград", code="UA01000000000000002")]
    village_label = [feature("place", **{"name:uk": "Староград", "id": "ua:b", "kind": "city"})]  # kind не має значення

    assert gz.rename_violations([RENAME], gazetteer(rows), village_label) == []


def test_rename_check_flags_an_old_name_held_by_another_entity_of_the_renamed_type() -> None:
    renamed_village = [
        row("ua:a", "selo", "Новоград", code="UA01000000000000001"),
        row("ua:b", "selo", "Староград", code="UA01000000000000002"),
    ]
    old_city = [row("ua:a", "city", "Новоград", code="UA01000000000000001"), row("ua:c", "city", "Староград", code="UA01000000000000003")]

    assert len(gz.rename_violations([RENAME], gazetteer(renamed_village), [])) == 1
    assert len(gz.rename_violations([RENAME], gazetteer(old_city), [])) == 1


def test_rename_check_gives_no_excuse_without_a_katottg_code_or_a_matching_entity_id() -> None:
    uncoded = [row("ua:a", "city", "Новоград", code="UA01000000000000001"), row("wd:Q1", "lake", "Староград")]
    clean = gazetteer([row("ua:a", "city", "Новоград", code="UA01000000000000001")])

    assert len(gz.rename_violations([RENAME], gazetteer(uncoded), [])) == 1
    assert len(gz.rename_violations([RENAME], clean, [feature("place", **{"name:uk": "Староград", "id": "ua:zzz"})])) == 1


@pytest.mark.parametrize("layer", ["place", "admin", "country", "water", "waterway", "peak"])
def test_rename_check_looks_for_the_old_name_in_every_layer_with_names(layer: str) -> None:
    clean = gazetteer([row("ua:a", "city", "Новоград", code="UA01000000000000001")])

    assert len(gz.rename_violations([RENAME], clean, [feature(layer, **{"name:uk": "Староград"})])) == 1


def test_rename_check_flags_a_missing_new_name() -> None:
    assert len(gz.rename_violations([RENAME], gazetteer([row("ua:a", "city", "Інше")]), [])) == 1


# --- лічильники й літери -----------------------------------------------------------------------------------------------

def test_counts_exclude_crimean_hromadas_and_count_k_cities_twice() -> None:
    rows = [
        row("a", "oblast", "О"), row("b", "city", "Київ", code="UA80000000000093317"), row("c", "city", "Місто", code="UA05000000000000001"),
        row("d", "hromada", "Г1", code="UA01000000000000002"), row("e", "hromada", "Г2", code="UA05000000000000003"), row("f", "raion", "Р"),
    ]  # fmt: skip

    assert gz.entity_counts(gazetteer(rows)) == {"oblast_plus_k": 2, "raion": 1, "hromada": 1, "city": 2}


def test_first_letter_takes_the_first_word_and_skips_apostrophes() -> None:
    assert gz.first_letter("Кам’янське") == "к"
    assert gz.first_letter("Нова Каховка") == "н"
    assert gz.first_letter("123") is None


def test_starting_letter_check_counts_pool_up_to_the_tier_and_ignores_the_soft_sign() -> None:
    rows = [row(f"ua:{i}", "city", f"К{i}", tier=2 if i else 1) for i in range(10)]
    ok = {1: frozenset(), 2: frozenset({"к"})}
    assert gz.starting_letter_violations(gazetteer(rows), ok, 10) == []

    weak = {1: frozenset({"к"}), 2: frozenset({"к", gz.SOFT_SIGN})}  # у ярусі 1 одна назва, «ь» ловить інша перевірка
    assert len(gz.starting_letter_violations(gazetteer(rows), weak, 10)) == 1


def test_chain_letter_check_uses_the_effective_pool_not_the_own_tier() -> None:
    startable = {1: frozenset(), 2: frozenset({"к"})}
    rows = [row("ua:a", "city", "А", tier=1, chain="к"), row("ua:b", "city", "Б", tier=1, chain="м")]

    found, checked = gz.chain_letter_violations(gazetteer(rows), startable)

    assert (len(found), checked) == (1, 2)
    assert "ua:b" in found[0]


def test_chain_letter_check_reports_a_tier_without_any_pool() -> None:
    found, _ = gz.chain_letter_violations(gazetteer([row("ua:a", "city", "А", tier=1, chain="к")]), {1: frozenset()})

    assert len(found) == 1


# --- геометрія ---------------------------------------------------------------------------------------------------------

def test_land_gaps_ignore_coast_slivers_but_catch_a_hole() -> None:
    mask = box(0, 0, 10 * KM, 10 * KM)
    land = box(0, 0, 10 * KM, 10 * KM - 20)  # смужка 20 м уздовж берега: спрощення берега
    total, largest = geo.land_gaps(land, mask)
    assert total == pytest.approx(0.2, abs=0.01) and largest == 0.0

    holed = land.difference(box(4 * KM, 4 * KM, 6 * KM, 6 * KM))
    assert geo.land_gaps(holed, mask)[1] == pytest.approx(4.0, abs=0.05)


def test_checked_zone_drops_only_components_touching_a_neutral_country() -> None:
    taman = box(20 * KM, 0, 30 * KM, 10 * KM)
    island = box(40 * KM, 0, 41 * KM, 1 * KM)  # острів, якого NE-Україна не містить, лишається в перевірці
    mainland = box(0, 0, 10 * KM, 10 * KM)
    foreign = box(25 * KM, 5 * KM, 35 * KM, 15 * KM)

    zone = geo.checked_zone(mainland.union(taman).union(island), foreign)

    assert zone.equals(mainland.union(island))


def test_anchors_outside_counts_points_far_from_the_zone() -> None:
    lon, lat = geo.CRIMEA_ANCHORS[0]
    x, y = geo.to_metric(np.array([lon]), np.array([lat]))
    near = box(x[0] - 100, y[0] - 100, x[0] + 100, y[0] + 100)

    assert geo.anchors_outside(near) == len(geo.CRIMEA_ANCHORS) - 1


NEIGHBOURS = box(-10 * KM, 0, 0, 10 * KM)
LAND = box(0, 0, 10 * KM, 10 * KM)


def test_outline_check_accepts_one_main_polygon_with_a_small_island() -> None:
    found, parts = geo.outline_violations(LAND.union(box(20 * KM, 0, 21 * KM, 1 * KM)), NEIGHBOURS)

    assert (found, parts) == ([], 2)


def test_outline_check_flags_a_second_part_touching_a_neighbour() -> None:
    second = box(20 * KM, 0, 21 * KM, 1 * KM)
    neighbours = NEIGHBOURS.union(box(21 * KM, 0, 25 * KM, 1 * KM))

    assert len(geo.outline_violations(LAND.union(second), neighbours)[0]) == 1


def test_outline_check_flags_a_cut_off_big_part_and_a_hole() -> None:
    far_big = box(20 * KM, 0, 20 * KM + 1000, 1000)  # лише щоб було що ділити
    huge = box(30 * KM, 0, 30 * KM + 30 * KM, 30 * KM)  # 900 км² без сусідів
    assert len(geo.outline_violations(LAND.union(far_big).union(huge), NEIGHBOURS)[0]) == 1

    holed = LAND.difference(box(5 * KM, 5 * KM, 5 * KM + 500, 5 * KM + 500))
    assert len(geo.outline_violations(holed, NEIGHBOURS)[0]) == 1


def test_border_gaps_separate_a_real_gap_from_a_coast_next_to_sea_overlap() -> None:
    border = LineString([(0, 0), (0, 4 * KM)])  # кордон покриває лише південну частину спільної межі
    everywhere_land = lambda _: box(-20 * KM, -20 * KM, 20 * KM, 20 * KM)  # noqa: E731
    sea_beside_land = lambda _: LAND  # noqa: E731  # сусід лежить на морі: суша OSM лише з боку України

    assert len(geo.border_gaps(LAND, NEIGHBOURS, border, everywhere_land)) == 1
    assert geo.border_gaps(LAND, NEIGHBOURS, border, sea_beside_land) == []
    assert geo.border_gaps(LAND, NEIGHBOURS, LineString([(0, 0), (0, 10 * KM)]), everywhere_land) == []


def test_border_deviation_weights_points_by_length_not_by_vertices() -> None:
    ne_home = box(1 * KM, 0, 10 * KM, 10 * KM)  # NE на 1 км східніше
    ne_neighbours = box(-10 * KM, 0, 1 * KM, 10 * KM)
    dense = LineString([(0, y) for y in range(0, 10001, 10)])  # тисяча вершин на кілометр: вершини не мусять переважувати

    sparse_report = geo.border_deviation(LineString([(0, 0), (0, 10 * KM)]), NEIGHBOURS, ne_home, ne_neighbours)
    dense_report = geo.border_deviation(dense, NEIGHBOURS, ne_home, ne_neighbours)

    assert dense_report["level0_to_ne"]["points"] == sparse_report["level0_to_ne"]["points"]
    assert dense_report["level0_to_ne"]["p50"] == pytest.approx(1.0, abs=0.01)


def test_border_gaps_refuse_an_empty_mask_instead_of_calling_a_gap_coast() -> None:
    border = LineString([(0, 0), (0, 4 * KM)])

    with pytest.raises(AssertionError):
        geo.border_gaps(LAND, NEIGHBOURS, border, lambda _: Polygon())


def test_border_deviation_reports_both_directions_and_the_threshold() -> None:
    ne_home = box(2 * KM, 0, 10 * KM, 10 * KM)  # межа NE на 2 км східніше за тайли
    ne_neighbours = box(-10 * KM, 0, 2 * KM, 10 * KM)
    border = LineString([(0, 0), (0, 10 * KM)])

    report = geo.border_deviation(border, NEIGHBOURS, ne_home, ne_neighbours)

    assert report["overall"]["p50"] == pytest.approx(2.0, abs=0.1)
    assert report["overall"]["max"] >= 2.0
    assert report["threshold_km"] == geo.MAX_DEVIATION_KM
    assert report["level0_to_ne"]["points"] > 0 and report["ne_to_level0"]["points"] > 0


def test_tiles_covering_returns_the_tiles_around_a_point() -> None:
    assert (12, *tile_xy(34.0, 45.0, 12)) in geo.tiles_covering(12, (33.99, 44.99, 34.01, 45.01))
