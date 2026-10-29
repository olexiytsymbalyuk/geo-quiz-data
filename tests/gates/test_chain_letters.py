"""Ворота: стартові літери ярусів і літери ланцюжка (Додаток А.1, А.4; Ruling 22).

`startable["1"] = []` — легітимно: тоді сутності ярусу 1 беруть пул найближчого непорожнього яруса.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from pipeline.policy import load_policy
from tests.gates.gate_support import Gazetteer, summarize
from tests.gates.gazetteer_support import (
    chain_letter_violations,
    parse_startable,
    soft_sign_violations,
    starting_letter_violations,
)


@pytest.fixture(scope="module")
def startable(gazetteer: Gazetteer) -> Mapping[int, frozenset[str]]:
    parsed = parse_startable(gazetteer)
    assert parsed, "у meta немає startable — перевірка була б порожньою"
    assert any(parsed.values()), "жоден ярус не має стартових літер — перевірка була б порожньою"
    return parsed


def test_soft_sign_is_never_startable(startable: Mapping[int, frozenset[str]]) -> None:
    found = soft_sign_violations(startable)

    assert not found, summarize(found)


def test_every_startable_letter_has_enough_names(gazetteer: Gazetteer, startable: Mapping[int, frozenset[str]]) -> None:
    found = starting_letter_violations(gazetteer, startable, load_policy().startable_min)

    assert not found, summarize(found)


def test_chain_letters_belong_to_effective_pool(gazetteer: Gazetteer, startable: Mapping[int, frozenset[str]]) -> None:
    found, checked = chain_letter_violations(gazetteer, startable)

    assert checked > 0, "у газетирі немає населених пунктів з ярусом — перевірка порожня"
    assert not found, summarize(found)
