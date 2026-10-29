"""Ворота: у релізі немає `name:ru`, голого `name` і жодного `name:*`, крім `name:uk`."""

from __future__ import annotations

from tests.gates.gate_support import (
    Gazetteer,
    JsonArtifact,
    TileSet,
    gazetteer_key_violations,
    json_key_violations,
    summarize,
    tile_key_violations,
)


def test_tiles_have_no_name_ru_key(tiles: TileSet) -> None:
    assert tiles.attributes, "у PMTiles немає жодної фічі — перевірка була б порожньою"
    found = tile_key_violations(tiles.attributes)

    assert not found, summarize(found)


def test_gazetteer_names_have_no_ru(gazetteer: Gazetteer) -> None:
    assert gazetteer.entities, "у газетирі немає сутностей — перевірка була б порожньою"
    found = gazetteer_key_violations(gazetteer)

    assert not found, summarize(found)


def test_gazetteer_json_has_no_ru_key(gazetteer_json: JsonArtifact) -> None:
    assert gazetteer_json.payload, "JSON газетира порожній — перевірка була б порожньою"
    found = json_key_violations(gazetteer_json.payload, gazetteer_json.name)

    assert not found, summarize(found)
