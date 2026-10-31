"""Ворота: назви — лише за КАТОТТГ, перейменування застосовані (Додаток Б, `test_occupied_uses_katottg`).

Еталон КАТОТТГ розбирається з сирого кодифікатора (`--raw`), а не береться з газетира: збірка не перевіряє себе сама.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.katottg import parse_katottg
from pipeline.policy import load_policy
from tests.gates.gate_support import Gazetteer, JsonArtifact, PolicyView, TileSet, find_one, summarize
from tests.gates.gazetteer_support import json_old_name_violations, katottg_name_violations, rename_violations


@pytest.fixture(scope="module")
def katottg_xlsx(raw_dir: Path) -> Path:
    return find_one(raw_dir / "katottg", "*.xlsx")


def test_names_equal_katottg(gazetteer: Gazetteer, katottg_xlsx: Path) -> None:
    units = parse_katottg(katottg_xlsx)
    assert units, "кодифікатор порожній — перевірка була б порожньою"
    found, checked = katottg_name_violations(gazetteer, units)

    assert checked > 0, "у газетирі немає сутностей з кодом КАТОТТГ — перевірка порожня"
    assert not found, summarize(found)


def test_renamed_places_use_new_names(gazetteer: Gazetteer, tiles: TileSet, policy: PolicyView) -> None:
    renames = load_policy().renames_check
    assert renames, "у policy.yaml немає renames_check — перевірка була б порожньою"
    assert tiles.attributes, "у PMTiles немає жодної фічі — перевірка була б порожньою"
    found = rename_violations(renames, policy.renames_homonym_exceptions, gazetteer, tiles.attributes)

    assert not found, summarize(found)


def test_old_names_stay_out_of_scope_json(gazetteer_json: JsonArtifact, policy: PolicyView) -> None:
    renames = load_policy().renames_check
    assert gazetteer_json.payload.get("entities"), "у JSON газетира немає сутностей — перевірка була б порожньою"
    found = json_old_name_violations(gazetteer_json.payload, renames, gazetteer_json.name, policy.renames_homonym_exceptions)

    assert not found, summarize(found)
