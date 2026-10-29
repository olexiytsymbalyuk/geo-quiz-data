"""Ворота: кількість одиниць за газетиром збігається з Додатком Б (± 2); більший дрейф — ручний перегляд."""

from __future__ import annotations

import pytest

from tests.gates.gate_support import Gazetteer
from tests.gates.gazetteer_support import COUNT_TOLERANCE, EXPECTED_COUNTS, entity_counts


@pytest.mark.parametrize("kind", sorted(EXPECTED_COUNTS))
def test_entity_count(kind: str, gazetteer: Gazetteer) -> None:
    """O+K = областей + K-міст; H — громади без Криму (коди UA01…); M — усі міста, K теж."""
    assert gazetteer.entities, "у газетирі немає сутностей — перевірка була б порожньою"
    actual = entity_counts(gazetteer)[kind]
    expected = EXPECTED_COUNTS[kind]

    assert abs(actual - expected) <= COUNT_TOLERANCE, (
        f"{kind}: {actual}, очікується {expected} ± {COUNT_TOLERANCE}"
    )
