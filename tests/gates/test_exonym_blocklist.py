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
    assert policy.blocked_sentinel_key in policy.blocked
    assert len(policy.blocked) >= len(load_policy().exonym_blocklist) // 2


def test_tiles_have_no_blocked_names(tiles: TileSet, policy: PolicyView) -> None:
    found = tile_exonym_violations(tiles.attributes, policy.blocked)

    assert not found, summarize(found)


def test_sqlite_has_no_blocked_names(gazetteer: Gazetteer, policy: PolicyView) -> None:
    found = gazetteer_exonym_violations(gazetteer, policy.blocked)

    assert not found, summarize(found)


def test_json_has_no_blocked_names(gazetteer_json: JsonArtifact, policy: PolicyView) -> None:
    found = json_exonym_violations(gazetteer_json.payload, policy.blocked)

    assert not found, summarize(found)
