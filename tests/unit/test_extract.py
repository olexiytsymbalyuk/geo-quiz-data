"""Витяг OSM: білий список тегів і потокова обробка GeoJSONSeq."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.extract import KEEP_TAGS, clean_feature, clean_geojsonseq, strip_tags


def test_strip_tags_never_keeps_bare_or_foreign_names() -> None:
    tags = {"name": "x", "name:ru": "x", "name:en": "x", "name:uk": "Ялта", "katotth": "UA01000000000013043", "old_name": "x"}
    assert strip_tags(tags) == {"name:uk": "Ялта", "katotth": "UA01000000000013043"}


def test_keep_tags_is_exact_whitelist() -> None:
    assert KEEP_TAGS == frozenset(
        {"katotth", "name:uk", "place", "admin_level", "boundary", "natural", "waterway", "water", "wikidata", "ele", "population"}
    )


def test_clean_feature_keeps_osm_ref_and_geometry_and_drops_forbidden_tags() -> None:
    geometry = {"type": "Point", "coordinates": [34.17, 44.49]}
    feature = {
        "type": "Feature",
        "properties": {
            "@type": "node",
            "@id": 123,
            "name": "x",
            "name:ru": "x",
            "name:uk": "Ялта",
            "place": "city",
            "katotth": "UA01000000000013043",
            "population": "76746",
            "source": "x",
        },
        "geometry": geometry,
    }
    cleaned = clean_feature(feature)
    assert cleaned["type"] == "Feature"
    assert cleaned["geometry"] == geometry
    assert cleaned["properties"] == {
        "@type": "node",
        "@id": 123,
        "name:uk": "Ялта",
        "place": "city",
        "katotth": "UA01000000000013043",
        "population": "76746",
    }
    assert "name" in feature["properties"]  # вхідний словник не змінюється


def test_clean_feature_without_properties() -> None:
    cleaned = clean_feature({"type": "Feature", "geometry": None})
    assert cleaned["properties"] == {}
    assert cleaned["geometry"] is None


def test_clean_geojsonseq_streams_lines_with_and_without_record_separator(tmp_path: Path) -> None:
    def feat(i: int, **tags: str) -> dict[str, object]:
        return {
            "type": "Feature",
            "properties": {"@type": "node", "@id": i, **tags},
            "geometry": {"type": "Point", "coordinates": [30.0, 50.0]},
        }

    src = tmp_path / "raw.geojsonseq"
    src.write_text(
        "\x1e" + json.dumps(feat(1, name="x", place="city"), ensure_ascii=False) + "\n"
        + "\n"
        + json.dumps(feat(2, **{"name:ru": "x", "name:uk": "Київ"}), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    dst = tmp_path / "out" / "clean.geojsonl"
    assert clean_geojsonseq(src, dst) == (2, 0)
    lines = dst.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["properties"] for line in lines] == [
        {"@type": "node", "@id": 1, "place": "city"},
        {"@type": "node", "@id": 2, "name:uk": "Київ"},
    ]
    assert "\x1e" not in dst.read_text(encoding="utf-8")


def test_clean_geojsonseq_counts_katotth(tmp_path: Path) -> None:
    src = tmp_path / "raw.geojsonseq"
    rows = [
        {"type": "Feature", "properties": {"@type": "node", "@id": 1, "katotth": "UA01"}, "geometry": None},
        {"type": "Feature", "properties": {"@type": "node", "@id": 2}, "geometry": None},
    ]
    src.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert clean_geojsonseq(src, tmp_path / "o.geojsonl") == (2, 1)


def _f(**props: object) -> dict[str, object]:
    return {"type": "Feature", "properties": props, "geometry": None}


def test_layer_filters_drop_members_pulled_in_by_tags_filter() -> None:
    from pipeline.extract import is_admin, is_peak, is_place, is_water

    assert is_admin(_f(**{"@type": "relation", "@id": 1, "boundary": "administrative"}))
    assert not is_admin(_f(**{"@type": "way", "@id": 2, "natural": "coastline"}))
    assert is_place(_f(place="village"))
    assert not is_place(_f(natural="peak"))
    assert is_water(_f(natural="water"))
    assert is_water(_f(waterway="canal"))
    assert not is_water(_f(waterway="dam"))
    assert not is_water(_f(natural="wetland"))
    assert is_peak(_f(natural="peak"))
    assert not is_peak(_f(natural="water"))


def test_clean_geojsonseq_applies_keep_predicate(tmp_path: Path) -> None:
    src = tmp_path / "raw.geojsonseq"
    rows = [
        {"type": "Feature", "properties": {"@type": "relation", "@id": 1, "katotth": "UA01"}, "geometry": None},
        {"type": "Feature", "properties": {"@type": "way", "@id": 2, "katotth": "UA02"}, "geometry": None},
    ]
    src.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    from pipeline.extract import is_admin

    assert clean_geojsonseq(src, tmp_path / "o.geojsonl", keep=is_admin) == (1, 1)
    assert len((tmp_path / "o.geojsonl").read_text(encoding="utf-8").splitlines()) == 1
