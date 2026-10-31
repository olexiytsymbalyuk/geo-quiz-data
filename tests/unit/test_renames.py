"""apply_renames: колишні назви потрапляють у names.former[] перейменованої сутності."""

from __future__ import annotations

from collections import Counter

import pytest

from pipeline.gazetteer import Entity, GazetteerError
from pipeline.policy import Rename
from pipeline.renames import apply_renames

OLD = "Старівка"
NEW = "Новівка"
CODE_A = "UA01020010010000004"
CODE_B = "UA01020010020000005"


def entity(entity_id: str, kind: str, name: str, katottg: str | None = None) -> Entity:
    names = {"uk": name, "en": None, "uk_aliases": [], "en_aliases": [], "local": None, "former": []}
    return Entity(id=entity_id, type=kind, names=names, katottg=katottg)


def rename(old: str = OLD, new: str = NEW, kind: str = "soviet", katottg: str | None = None) -> Rename:
    return Rename(old=old, new=new, date="2016-05-19", source="тест", kind=kind, katottg=katottg)


class _Guard:
    def __init__(self, blocked: str | None = None) -> None:
        self._blocked = blocked

    def check(self, name: str, entity_id: str) -> None:
        if name == self._blocked:
            raise GazetteerError(f"заборонена назва сутності {entity_id}")


def apply(entities: list[Entity], renames: list[Rename], guard: _Guard | None = None, metrics: Counter[str] | None = None) -> list[Entity]:
    return apply_renames(entities, renames, guard or _Guard(), Counter() if metrics is None else metrics)


def test_former_name_is_attached_to_the_renamed_entity() -> None:
    result = apply([entity("ua:1", "selo", NEW, CODE_A)], [rename()])
    assert result[0].names["former"] == [{"name": OLD, "kind": "soviet", "until": "2016-05-19"}]


def test_primary_type_wins_when_city_and_village_share_the_new_name() -> None:
    entities = [entity("ua:1", "selo", NEW, CODE_A), entity("ua:2", "city", NEW, CODE_B)]
    result = apply(entities, [rename()])
    assert result[0].names["former"] == []
    assert result[1].names["former"] == [{"name": OLD, "kind": "soviet", "until": "2016-05-19"}]


def test_ambiguous_new_name_within_primary_type_fails_with_pair_number_only() -> None:
    entities = [entity("ua:1", "selo", NEW, CODE_A), entity("ua:2", "selo", NEW, CODE_B)]
    with pytest.raises(GazetteerError) as caught:
        apply(entities, [rename()])
    text = str(caught.value)
    assert "№1" in text
    assert NEW not in text and OLD not in text


def test_katottg_in_pair_resolves_ambiguity() -> None:
    entities = [entity("ua:1", "selo", NEW, CODE_A), entity("ua:2", "selo", NEW, CODE_B)]
    result = apply(entities, [rename(katottg=CODE_B)])
    assert result[0].names["former"] == []
    assert [f["name"] for f in result[1].names["former"]] == [OLD]


def test_katottg_not_found_among_candidates_fails() -> None:
    with pytest.raises(GazetteerError) as caught:
        apply([entity("ua:1", "selo", NEW, CODE_A)], [rename(katottg=CODE_B)])
    assert "№1" in str(caught.value)


def test_missing_new_name_fails() -> None:
    with pytest.raises(GazetteerError) as caught:
        apply([entity("ua:1", "selo", "Інша", CODE_A)], [rename()])
    text = str(caught.value)
    assert "№1" in text
    assert NEW not in text and OLD not in text


def test_former_name_passes_name_guard() -> None:
    entities = [entity("ua:1", "selo", NEW, CODE_A)]
    with pytest.raises(GazetteerError):
        apply(entities, [rename()], _Guard(blocked=OLD))
    assert entities[0].names["former"] == []


def test_metrics_count_former_names() -> None:
    entities = [entity("ua:1", "selo", NEW, CODE_A), entity("ua:2", "city", "Нововів", CODE_B)]
    metrics: Counter[str] = Counter()
    apply(entities, [rename(), rename(old="Давнів", new="Нововів")], metrics=metrics)
    assert metrics["former_names_total"] == 2


def test_two_pairs_for_one_entity_keep_both_former_names() -> None:
    result = apply([entity("ua:1", "selo", NEW, CODE_A)], [rename(), rename(old="Давнів", kind="imperial")])
    assert [f["name"] for f in result[0].names["former"]] == [OLD, "Давнів"]


def test_original_entities_are_not_mutated_and_untouched_ones_keep_identity() -> None:
    entities = [entity("ua:1", "selo", "Інша", CODE_B), entity("ua:2", "selo", NEW, CODE_A)]
    result = apply(entities, [rename()])
    assert result[0] is entities[0]
    assert result[1] is not entities[1]
    assert entities[1].names["former"] == []


def test_non_settlement_entities_are_untouched() -> None:
    entities = [entity("water:1", "river", NEW)]
    with pytest.raises(GazetteerError) as caught:
        apply(entities, [rename()])
    assert "№1" in str(caught.value)
    assert entities[0].names["former"] == []
