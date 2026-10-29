"""Газетир «Україна»: КАТОТТГ + OSM + Wikidata (Завдання 10)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import shapely.geometry

from pipeline.gazetteer import (
    CAPITAL_OVERRIDES,
    Entity,
    GazetteerError,
    GazetteerResult,
    build_gazetteer,
    build_report,
    drop_peaks_outside,
    effective_chain_tier,
    resolve_capitals,
    water_entity_id,
)
from pipeline.gazetteer_io import write_scope_json, write_sqlite
from pipeline.katottg import KatottgUnit
from pipeline.policy import Policy, load_policy
from pipeline.wikidata import WdItem

POLICY: Policy = dataclasses.replace(load_policy(), startable_min=1)
VERSION = "2026.11.1"

OBL = "UA01000000000000001"
RAI = "UA01020000000000002"
HRO = "UA01020010000000003"
CAP = "UA01020010010000004"  # обласний центр, M, 40 тис.
BIG = "UA01020010020000005"  # M, 120 тис.
MID = "UA01020010030000006"  # M, 50 тис.
SEL = "UA01020010040000007"  # X
VIL = "UA01020010050000008"  # C
KYV = "UA80000000000000009"  # K
DST = "UA01020010010000010"  # B

UNITS = [
    KatottgUnit(OBL, "O", "Тестівська область", ()),
    KatottgUnit(RAI, "P", "Тестівський район", (OBL,)),
    KatottgUnit(HRO, "H", "Тестівська громада", (OBL, RAI)),
    KatottgUnit(CAP, "M", "Центрівка", (OBL, RAI, HRO)),
    KatottgUnit(BIG, "M", "Великів", (OBL, RAI, HRO)),
    KatottgUnit(MID, "M", "Середнів", (OBL, RAI, HRO)),
    KatottgUnit(SEL, "X", "Селищів", (OBL, RAI, HRO)),
    KatottgUnit(VIL, "C", "Сілів", (OBL, RAI, HRO)),
    KatottgUnit(KYV, "K", "Столиця", ()),
    KatottgUnit(DST, "B", "Район міста", (OBL, RAI, HRO, CAP)),
]


def pt(osm_id: int, code: str | None, place: str, lon: float, lat: float, kind: str = "node", **tags: str) -> dict[str, Any]:
    props: dict[str, Any] = {"@type": kind, "@id": osm_id, "place": place, "name:uk": "OSM-назва", **tags}
    if code:
        props["katotth"] = code
    return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def rel(osm_id: int, code: str | None, ring: list[list[float]], **tags: str) -> dict[str, Any]:
    props: dict[str, Any] = {"@type": "relation", "@id": osm_id, "boundary": "administrative", **tags}
    if code:
        props["katotth"] = code
    return {"type": "Feature", "properties": props, "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]}}


SQUARE = [[30.0, 50.0], [32.0, 50.0], [32.0, 52.0], [30.0, 52.0], [30.0, 50.0]]


def line(osm_id: int, name: str | None, kind: str = "way", **tags: str) -> dict[str, Any]:
    props: dict[str, Any] = {"@type": kind, "@id": osm_id, **tags}
    if name is not None:
        props["name:uk"] = name
    return {"type": "Feature", "properties": props, "geometry": {"type": "LineString", "coordinates": [[30, 50], [31, 51]]}}


def peak(osm_id: int, name: str | None, **tags: str) -> dict[str, Any]:
    props: dict[str, Any] = {"@type": "node", "@id": osm_id, "natural": "peak", **tags}
    if name is not None:
        props["name:uk"] = name
    return {"type": "Feature", "properties": props, "geometry": {"type": "Point", "coordinates": [24.0, 48.0]}}


def wd_item(qid: str, population: int | None = None, lat: float | None = None, lon: float | None = None) -> WdItem:
    return WdItem(qid=qid, population=population, population_year=2022 if population is not None else None, lat=lat, lon=lon)


def build(
    *,
    units: list[KatottgUnit] | None = None,
    places: list[dict[str, Any]] | None = None,
    admin: list[dict[str, Any]] | None = None,
    water: list[dict[str, Any]] | None = None,
    peaks: list[dict[str, Any]] | None = None,
    wd: dict[str, WdItem] | None = None,
    capitals: dict[str, str] | None = None,
    policy: Policy = POLICY,
) -> GazetteerResult:
    return build_gazetteer(
        UNITS if units is None else units,
        places or [],
        admin or [],
        water or [],
        peaks or [],
        wd or {},
        capitals or {},
        policy,
        VERSION,
    )


def by_id(result: GazetteerResult) -> dict[str, Entity]:
    return {e.id: e for e in result.entities}


# --- точки й населені пункти ---


def test_city_gets_osm_point_and_ids() -> None:
    result = build(places=[pt(11, SEL, "town", 33.5, 49.5)])
    e = by_id(result)[f"ua:{SEL}"]
    assert (e.lat, e.lon) == (49.5, 33.5)
    assert e.osm_id == "n11"
    assert e.katottg == SEL
    assert e.type == "selyshche"
    assert e.country == "UA"
    assert e.names["uk"] == "Селищів"  # назва лише з КАТОТТГ, не з OSM
    assert e.scopes == ["ukraine"]
    assert e.dataset_version == VERSION


def test_city_without_osm_uses_wikidata_coordinates() -> None:
    result = build(wd={SEL: wd_item("Q1", lat=48.0, lon=34.0)})
    e = by_id(result)[f"ua:{SEL}"]
    assert (e.lat, e.lon) == (48.0, 34.0)
    assert e.qid == "Q1"
    assert e.osm_id is None


def test_city_without_any_point_counts_no_point() -> None:
    result = build(places=[pt(1, SEL, "town", 33.5, 49.5)], wd={VIL: wd_item("Q2", lat=1.0, lon=2.0)})
    e = by_id(result)[f"ua:{MID}"]
    assert e.lat is None and e.lon is None
    # без точки: CAP, BIG, MID, KYV (SEL має OSM, VIL - Wikidata)
    assert result.metrics["no_point"] == 4


def test_osm_code_missing_in_katottg_is_unknown_and_dropped() -> None:
    result = build(places=[pt(1, "UA99999999999999999", "town", 33.0, 49.0)])
    assert result.metrics["unknown_katotth"] == 1
    assert not any(e.osm_id == "n1" for e in result.entities)


def test_duplicate_katotth_picks_highest_place_rank() -> None:
    places = [
        pt(5, SEL, "village", 30.0, 50.0),
        pt(6, SEL, "town", 31.0, 51.0),
    ]
    result = build(places=places)
    e = by_id(result)[f"ua:{SEL}"]
    assert e.osm_id == "n6"
    assert (e.lat, e.lon) == (51.0, 31.0)
    assert result.metrics["duplicate_katotth"] == 1
    assert sum(1 for x in result.entities if x.katottg == SEL) == 1


def test_duplicate_equal_rank_prefers_node_then_smaller_id() -> None:
    way = pt(2, SEL, "town", 30.0, 50.0, kind="way")
    way["geometry"] = {"type": "Polygon", "coordinates": [SQUARE]}
    result = build(places=[way, pt(9, SEL, "town", 31.0, 51.0), pt(8, SEL, "town", 32.0, 52.0)])
    assert by_id(result)[f"ua:{SEL}"].osm_id == "n8"
    assert result.metrics["duplicate_katotth"] == 2


def test_place_relation_in_admin_layer_gives_representative_point() -> None:
    result = build(admin=[rel(77, SEL, SQUARE, place="town")])
    e = by_id(result)[f"ua:{SEL}"]
    assert e.osm_id == "r77"
    assert e.lat is not None and 50.0 < e.lat < 52.0
    assert e.lon is not None and 30.0 < e.lon < 32.0


def test_places_layer_wins_over_admin_relation() -> None:
    result = build(places=[pt(3, SEL, "village", 33.0, 49.0)], admin=[rel(77, SEL, SQUARE, place="town")])
    assert by_id(result)[f"ua:{SEL}"].osm_id == "n3"


# --- яруси й обласні центри ---


def test_tiers() -> None:
    result = build(
        wd={BIG: wd_item("Q3", population=120_000), CAP: wd_item("Q4", population=40_000), MID: wd_item("Q5", population=50_000)},
        capitals={OBL: CAP},
    )
    got = by_id(result)
    assert got[f"ua:{CAP}"].tiers == {"ukraine": 1}
    assert got[f"ua:{CAP}"].attributes["is_oblast_centre"] is True
    assert got[f"ua:{BIG}"].tiers == {"ukraine": 1}
    assert got[f"ua:{MID}"].tiers == {"ukraine": 2}
    assert got[f"ua:{SEL}"].tiers == {"ukraine": 3}
    assert got[f"ua:{VIL}"].tiers == {"ukraine": 4}
    assert got[f"ua:{KYV}"].tiers == {"ukraine": 1}  # усі K - ярус 1
    assert got[f"ua:{MID}"].attributes["is_oblast_centre"] is False
    for non_settlement in (OBL, RAI, HRO, DST):
        assert got[f"ua:{non_settlement}"].tiers == {}
    assert result.metrics["tiers"] == {"1": 3, "2": 1, "3": 1, "4": 1}


def test_category_to_type_mapping() -> None:
    got = by_id(build())
    assert {k.split(":")[1]: v.type for k, v in got.items()} == {
        OBL: "oblast",
        RAI: "raion",
        HRO: "hromada",
        CAP: "city",
        BIG: "city",
        MID: "city",
        SEL: "selyshche",
        VIL: "selo",
        KYV: "city",
        DST: "city_district",
    }


def test_parents_are_nearest_first_as_ids() -> None:
    e = by_id(build())[f"ua:{CAP}"]
    assert e.parents == [f"ua:{HRO}", f"ua:{RAI}", f"ua:{OBL}"]
    assert by_id(build())[f"ua:{OBL}"].parents == []


def test_resolve_capitals_passthrough_and_override() -> None:
    donetsk_oblast, donetsk_city = next(iter(CAPITAL_OVERRIDES.items()))
    resolved = resolve_capitals({OBL: CAP}, {donetsk_oblast: ["UA1", "UA2"]})
    assert resolved == {OBL: CAP, donetsk_oblast: donetsk_city}


def test_resolve_capitals_conflict_without_override_raises() -> None:
    with pytest.raises(GazetteerError) as err:
        resolve_capitals({}, {OBL: [CAP, BIG]})
    assert OBL in str(err.value)


def test_resolve_capitals_ignores_non_oblast_keys() -> None:
    resolved = resolve_capitals({OBL: CAP, KYV: KYV}, {KYV: [KYV, CAP]}, oblast_codes=frozenset({OBL}))
    assert resolved == {OBL: CAP}


def test_build_ignores_non_oblast_capital_keys() -> None:
    result = build(capitals={KYV: CAP})
    assert by_id(result)[f"ua:{CAP}"].attributes["is_oblast_centre"] is False


# --- населення ---


def test_population_prefers_wikidata_then_osm_tag() -> None:
    result = build(
        places=[
            pt(1, SEL, "town", 30.0, 50.0, population="1234"),
            pt(2, VIL, "village", 30.0, 50.0, population="oops"),
            pt(3, MID, "town", 30.0, 50.0, population="77"),
        ],
        wd={MID: wd_item("Q9", population=5000)},
    )
    got = by_id(result)
    assert got[f"ua:{SEL}"].population == {"value": 1234, "year": None, "source": "osm"}
    assert got[f"ua:{VIL}"].population is None
    assert got[f"ua:{MID}"].population == {"value": 5000, "year": 2022, "source": "wikidata"}
    assert result.metrics["population_wd"] == 1
    assert result.metrics["population_osm"] == 1
    assert result.metrics["population_none"] == 4  # CAP, BIG, VIL, KYV


def test_negative_osm_population_is_ignored() -> None:
    result = build(places=[pt(1, SEL, "town", 30.0, 50.0, population="-5")])
    assert by_id(result)[f"ua:{SEL}"].population is None


# --- адмінполігони ---


def test_admin_polygon_gives_geom_ref_and_representative_point() -> None:
    result = build(admin=[rel(500, OBL, SQUARE)])
    e = by_id(result)[f"ua:{OBL}"]
    assert e.geom_ref == "osm:r500"
    assert e.osm_id == "r500"
    assert e.lat is not None and 50.0 < e.lat < 52.0
    assert e.lon is not None and 30.0 < e.lon < 32.0
    assert result.metrics["no_polygon"] == 2  # raion і hromada без полігона


def test_admin_without_polygon_does_not_fail() -> None:
    result = build()
    e = by_id(result)[f"ua:{RAI}"]
    assert e.geom_ref is None and e.lat is None and e.lon is None
    assert result.metrics["no_polygon"] == 3


def test_admin_unknown_code_is_counted() -> None:
    result = build(admin=[rel(500, "UA99999999999999999", SQUARE)])
    assert result.metrics["unknown_katotth"] == 1


def test_admin_duplicate_relations_pick_smallest_id() -> None:
    result = build(admin=[rel(600, RAI, SQUARE), rel(500, RAI, SQUARE)])
    assert by_id(result)[f"ua:{RAI}"].geom_ref == "osm:r500"
    assert result.metrics["duplicate_katotth"] == 1


# --- вода й вершини ---


def test_water_types_and_skips() -> None:
    water = [
        line(1, "Річка", waterway="river"),
        line(2, "Струмок", waterway="stream"),
        line(3, "Канал", waterway="canal"),
        line(4, "Озеро", natural="water", water="lake"),
        line(5, "Став", natural="water", water="pond"),
        line(6, "Водосховище", natural="water", water="reservoir"),
        line(7, "Лагуна", natural="water", water="lagoon"),
        line(8, "Лиман Тестовий", natural="water"),
        line(9, "Невідоме", natural="water"),
        line(10, None, waterway="river"),
    ]
    result = build(water=water)
    got = by_id(result)
    assert got["osm:w1"].type == "river"
    assert "osm:w2" not in got
    assert got["osm:w3"].type == "canal"
    assert got["osm:w4"].type == "lake"
    assert got["osm:w5"].type == "lake"
    assert got["osm:w6"].type == "reservoir"
    assert got["osm:w7"].type == "liman"
    assert got["osm:w8"].type == "liman"
    assert "osm:w9" not in got
    assert result.metrics["water_other"] == 2  # stream і безтипова вода
    assert result.metrics["water_no_name"] == 1
    e = got["osm:w1"]
    assert e.geom_ref == "osm:w1" and e.osm_id == "w1" and e.names["uk"] == "Річка"
    assert e.country == "UA" and e.scopes == ["ukraine"] and e.tiers == {} and e.chain_letter is None


def test_liman_by_separate_word_and_tag_priority() -> None:
    water = [
        line(1, "Тилігульський лиман", natural="water"),
        line(2, "Лиманський ставок", natural="water", water="lake"),
        line(3, "Лиманське водосховище", natural="water", water="reservoir"),
        line(4, "Лиман-1", natural="water", water="pond"),
        line(5, "Лиманне господарство", natural="water"),
        line(6, "Куяльницький ЛИМАН", natural="water", water="lake"),
    ]
    got = by_id(build(water=water))
    assert got["osm:w1"].type == "liman"
    assert got["osm:w2"].type == "lake"
    assert got["osm:w3"].type == "reservoir"
    assert got["osm:w4"].type == "lake"
    assert "osm:w5" not in got  # префікс без окремого слова «лиман» — не лиман
    assert got["osm:w6"].type == "liman"


def test_water_mixed_type_counts_groups_with_different_types() -> None:
    water = [
        line(1, "Тест", waterway="river", wikidata="Q7"),
        line(2, "Тест", waterway="canal", wikidata="Q7"),
        line(3, "Інша", waterway="river", wikidata="Q8"),
    ]
    result = build(water=water)
    assert result.metrics["water_mixed_type"] == 1
    assert by_id(result)["wd:Q7"].type == "river"


def test_population_exactly_100000_is_tier_one() -> None:
    result = build(wd={MID: wd_item("Q5", population=100_000), BIG: wd_item("Q6", population=99_999)})
    assert by_id(result)[f"ua:{MID}"].tiers == {"ukraine": 1}
    assert by_id(result)[f"ua:{BIG}"].tiers == {"ukraine": 2}


def test_write_scope_json_cleans_part_file_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "scope.json"
    target.mkdir()
    (target / "keep").write_text("x", encoding="utf-8")  # replace у непорожню теку впаде
    with pytest.raises(OSError):
        write_scope_json(build(), target)
    assert not (tmp_path / "scope.json.part").exists()


def test_water_pieces_with_same_wikidata_merge_into_one_entity() -> None:
    water = [
        line(30, "Тест", waterway="river", wikidata="Q100"),
        line(20, "Тест", waterway="river", wikidata="Q100"),
        line(25, "Тест", kind="relation", waterway="river", wikidata="Q100"),
    ]
    result = build(water=water)
    rivers = [e for e in result.entities if e.type == "river"]
    assert len(rivers) == 1
    assert rivers[0].id == "wd:Q100"
    assert rivers[0].qid == "Q100"
    assert rivers[0].geom_ref == "osm:w20"
    assert rivers[0].osm_id == "w20"


def test_peaks_have_elevation_and_need_a_name() -> None:
    result = build(peaks=[peak(1, "Говерла", ele="2061", wikidata="Q1"), peak(2, "Без висоти"), peak(3, None), peak(4, "Крива", ele="abc")])
    got = by_id(result)
    assert got["wd:Q1"].type == "peak"
    assert got["wd:Q1"].attributes["ele"] == 2061
    assert (got["wd:Q1"].lat, got["wd:Q1"].lon) == (48.0, 24.0)
    assert "ele" not in got["osm:n2"].attributes
    assert "ele" not in got["osm:n4"].attributes
    assert result.metrics["peak_no_name"] == 1


# --- перевірки назв ---


def test_forbidden_letter_in_water_name_fails_build_with_id_only() -> None:
    bad = "Тест\u044b"
    with pytest.raises(GazetteerError) as err:
        build(water=[line(77, bad, waterway="river")])
    assert "osm:w77" in str(err.value)
    assert bad not in str(err.value)


def test_blocked_sentinel_in_water_name_fails() -> None:
    with pytest.raises(GazetteerError) as err:
        build(water=[line(78, POLICY.blocked_sentinel.upper(), waterway="river")])
    assert "osm:w78" in str(err.value)
    assert POLICY.blocked_sentinel not in str(err.value).lower()


def test_peaks_outside_the_outline_are_dropped_and_counted() -> None:
    inside, outside = peak(1, "Всередині", ele="100"), peak(2, "Поза контуром", ele="200")
    for feature, lon in ((inside, 33.0), (outside, 40.0)):
        feature["geometry"] = {"type": "Point", "coordinates": [lon, 49.0]}
    result = build(units=UNITS, places=[pt(11, SEL, "town", 33.5, 49.5)], peaks=[inside, outside])
    outline = shapely.geometry.MultiPolygon([shapely.geometry.box(30.0, 45.0, 36.0, 52.0)])

    clipped = drop_peaks_outside(result, outline)

    peaks = [e for e in clipped.entities if e.type == "peak"]
    assert [e.names["uk"] for e in peaks] == ["Всередині"]
    assert clipped.metrics["peak_outside"] == 1
    assert clipped.metrics["entities"] == len(result.entities) - 1
    assert clipped.metrics["entities_by_type"]["peak"] == 1
    assert len(result.entities) == len(clipped.entities) + 1  # вхідний результат не змінено
    assert build_report(clipped)["peak_outside"] == 1


def test_peaks_inside_the_outline_are_kept_with_zero_outside_count() -> None:
    result = build(peaks=[peak(1, "Говерла", ele="2061")])
    outline = shapely.geometry.MultiPolygon([shapely.geometry.box(-180.0, -90.0, 180.0, 90.0)])

    clipped = drop_peaks_outside(result, outline)

    assert [e.type for e in clipped.entities] == [e.type for e in result.entities]
    assert clipped.metrics["peak_outside"] == 0


def test_blocked_sentinel_in_katottg_or_peak_name_fails() -> None:
    bad_unit = KatottgUnit(SEL, "X", POLICY.blocked_sentinel, (OBL,))
    with pytest.raises(GazetteerError) as err:
        build(units=[bad_unit])
    assert SEL in str(err.value)
    with pytest.raises(GazetteerError):
        build(peaks=[peak(5, POLICY.blocked_sentinel)])


def test_forbidden_letter_in_katottg_name_fails() -> None:
    bad_unit = KatottgUnit(SEL, "X", "Сел\u044dще", (OBL,))
    with pytest.raises(GazetteerError):
        build(units=[bad_unit])


# --- ланцюжкові літери ---


def test_chain_letters_belong_to_startable_of_their_tier() -> None:
    result = build(wd={BIG: wd_item("Q3", population=120_000)})
    assert set(result.startable) == {1, 2, 3, 4}
    for e in result.entities:
        tier = e.tiers.get("ukraine")
        if tier is None:
            assert e.chain_letter is None
        else:
            assert e.chain_letter in result.startable[tier], e.id
    assert result.startable[1] <= result.startable[2] <= result.startable[3] <= result.startable[4]


def test_chain_letter_value() -> None:
    e = by_id(build())[f"ua:{VIL}"]  # «Сілів» -> «в»
    assert e.chain_letter == "в"


def test_tier_one_letter_falls_back_to_smallest_nonempty_pool() -> None:
    units = [
        KatottgUnit(OBL, "O", "Тестівська область", ()),
        KatottgUnit(KYV, "K", "Адам", ()),
        KatottgUnit(CAP, "M", "Бадам", (OBL,)),
        KatottgUnit(BIG, "M", "Мам", (OBL,)),
        KatottgUnit(MID, "M", "Мом", (OBL,)),
    ]
    policy = dataclasses.replace(POLICY, startable_min=2)
    result = build(units=units, capitals={OBL: CAP}, policy=policy)
    got = by_id(result)
    assert result.startable[1] == frozenset()  # пул ярусу 1 малий: чесний вимір
    assert result.startable[2] == frozenset({"м"})
    assert effective_chain_tier(1, result.startable) == 2
    assert effective_chain_tier(2, result.startable) == 2
    for code in (KYV, CAP):
        assert got[f"ua:{code}"].tier == 1
        assert got[f"ua:{code}"].chain_letter in result.startable[2]
    assert result.metrics["chain_tier_fallback"] == 2
    assert result.metrics["chain_letter_not_startable"] == 0


def test_effective_chain_tier_raises_when_all_pools_empty() -> None:
    empty = {tier: frozenset[str]() for tier in (1, 2, 3, 4)}
    with pytest.raises(GazetteerError):
        effective_chain_tier(1, empty)
    with pytest.raises(GazetteerError):
        build(policy=dataclasses.replace(POLICY, startable_min=1000))


# --- запис ---


def test_entity_is_frozen() -> None:
    e = next(iter(build().entities))
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.lat = 1.0  # type: ignore[misc]


def test_build_is_deterministic() -> None:
    kwargs: dict[str, Any] = {
        "places": [pt(2, SEL, "town", 1.0, 2.0), pt(1, SEL, "town", 3.0, 4.0)],
        "water": [line(2, "Б", waterway="river"), line(1, "А", waterway="river")],
    }
    assert build(**kwargs).entities == build(**kwargs).entities


def test_write_scope_json_has_only_listed_keys(tmp_path: Path) -> None:
    result = build(
        places=[pt(1, SEL, "town", 33.5, 49.5)],
        wd={BIG: wd_item("Q3", population=120_000)},
        water=[line(1, "Річка", waterway="river")],
        peaks=[peak(1, "Гора")],
        admin=[rel(500, OBL, SQUARE)],
    )
    out = tmp_path / "scope.json"
    write_scope_json(result, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == {"dataset_version", "startable", "entities"}
    assert data["dataset_version"] == VERSION
    assert set(data["startable"]) == {"1", "2", "3", "4"}
    assert all(v == sorted(v) for v in data["startable"].values())
    assert all(set(e) == {"id", "type", "name", "lat", "lon", "tier", "population", "chain_letter"} for e in data["entities"])
    assert {e["type"] for e in data["entities"]} == {"oblast", "city", "selyshche", "selo"}
    by = {e["id"]: e for e in data["entities"]}
    assert by[f"ua:{SEL}"]["name"] == "Селищів" and by[f"ua:{SEL}"]["lat"] == 49.5 and by[f"ua:{SEL}"]["tier"] == 3
    assert by[f"ua:{BIG}"]["population"] == 120_000
    assert by[f"ua:{MID}"]["lat"] is None  # без точки - null, фільтрує фронтенд
    assert by[f"ua:{OBL}"]["tier"] is None


def test_write_sqlite_roundtrip(tmp_path: Path) -> None:
    result = build(
        places=[pt(1, SEL, "town", 33.5, 49.5)],
        water=[line(1, "Річка", waterway="river")],
        wd={SEL: wd_item("Q55", population=321)},
    )
    path = tmp_path / "g.sqlite"
    write_sqlite(result, path)
    write_sqlite(result, path)  # перезапис без падіння
    con = sqlite3.connect(path)
    try:
        assert con.execute("select count(*) from entity").fetchone()[0] == len(result.entities)
        row = con.execute("select names, tiers, lat, dataset_version, scopes from entity where id = ?", (f"ua:{SEL}",)).fetchone()
        assert json.loads(row[0])["uk"] == "Селищів"
        assert json.loads(row[1]) == {"ukraine": 3}
        assert row[2] == 49.5 and row[3] == VERSION and json.loads(row[4]) == ["ukraine"]
        assert "Селищів" in row[0]  # ensure_ascii=False
        assert "Селищів" in row[0]
        ident = con.execute(
            "select katottg, qid, osm_id, geom_ref, country, chain_letter, population, parents from entity where id = ?",
            (f"ua:{SEL}",),
        ).fetchone()
        assert ident[:3] == (SEL, "Q55", "n1") and ident[4] == "UA" and ident[5] is not None
        assert json.loads(ident[6]) == {"value": 321, "year": 2022, "source": "wikidata"}
        assert json.loads(ident[7]) == [f"ua:{HRO}", f"ua:{RAI}", f"ua:{OBL}"]
        assert con.execute("select geom_ref from entity where id = 'osm:w1'").fetchone() == ("osm:w1",)
        version, startable = con.execute("select dataset_version, startable_json from meta").fetchone()
        assert version == VERSION
        assert set(json.loads(startable)) == {"1", "2", "3", "4"}
    finally:
        con.close()


def test_water_entity_id_prefers_wikidata_and_falls_back_to_osm_ref() -> None:
    assert water_entity_id({"@type": "relation", "@id": 7, "wikidata": "Q42"}) == "wd:Q42"
    assert water_entity_id({"@type": "way", "@id": 8}) == "osm:w8"
    assert water_entity_id({"@type": "node", "@id": 9, "wikidata": "не-QID"}) == "osm:n9"
