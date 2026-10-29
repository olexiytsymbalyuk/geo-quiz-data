"""Ворота: жодна назва в релізі не збігається (після нормалізації §5.8) із записом блок-листа екзонімів."""

from __future__ import annotations

from pipeline.policy import load_policy
from tests.gates.gate_support import (
    Gazetteer,
    JsonArtifact,
    PolicyView,
    TileSet,
    gazetteer_exonym_violations,
    json_exonym_violations,
    summarize,
    tile_exonym_violations,
)


def test_blocklist_is_loaded_from_policy(policy: PolicyView) -> None:
    """Без непорожнього блок-листа решта перевірок були б порожніми."""
    has_sentinel = policy.blocked_sentinel_key in policy.blocked
    has_enough_keys = len(policy.blocked) >= len(load_policy().exonym_blocklist) // 2
    assert has_sentinel
    assert has_enough_keys


def test_tiles_have_no_blocked_names(tiles: TileSet, policy: PolicyView) -> None:
    assert tiles.attributes, "у PMTiles немає жодної фічі — перевірка була б порожньою"
    found = tile_exonym_violations(tiles.attributes, policy.blocked)

    assert not found, summarize(found)


def test_sqlite_has_no_blocked_names(gazetteer: Gazetteer, policy: PolicyView) -> None:
    assert gazetteer.entities, "у газетирі немає сутностей — перевірка була б порожньою"
    found = gazetteer_exonym_violations(gazetteer, policy.blocked)

    assert not found, summarize(found)


def test_json_has_no_blocked_names(gazetteer_json: JsonArtifact, policy: PolicyView) -> None:
    assert gazetteer_json.payload.get("entities"), "у JSON газетира немає сутностей — перевірка була б порожньою"
    found = json_exonym_violations(gazetteer_json.payload, policy.blocked)

    assert not found, summarize(found)
