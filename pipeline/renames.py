"""Колишні назви: пари перейменувань політики заповнюють `names.former[]` сутності з новою назвою.

Стара форма ніколи не стає основною назвою чи псевдонімом — лише записом `former`
(`{"name", "kind", "until"}`, бриф §5.3). У тексті помилок — лише порядковий номер пари:
назв у ньому немає.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from pipeline.policy import Rename

if TYPE_CHECKING:
    from pipeline.gazetteer import Entity

# Старшинство типів, з яких береться тип перейменованої сутності (серед однойменних пунктів із назвою `new`).
RENAMED_TYPE_RANK = ("city", "selyshche", "selo")


class NameGuard(Protocol):
    """Перевірка назви на заборонені літери й блок-лист екзонімів (кидає `GazetteerError`)."""

    def check(self, name: str, entity_id: str) -> None: ...


def _fail(number: int, reason: str) -> NoReturn:
    # Імпорт тут, бо `pipeline.gazetteer` сам імпортує цей модуль.
    from pipeline.gazetteer import GazetteerError

    raise GazetteerError(f"перейменування №{number}: {reason}")


def _candidate_index(entities: Sequence[Entity]) -> dict[str, list[int]]:
    """Позиції населених пунктів за українською назвою."""
    index: dict[str, list[int]] = {}
    for position, entity in enumerate(entities):
        if entity.type in RENAMED_TYPE_RANK:
            index.setdefault(str(entity.names["uk"]), []).append(position)
    return index


def _pick_target(number: int, rename: Rename, entities: Sequence[Entity], positions: list[int]) -> int:
    if not positions:
        _fail(number, "нової назви немає в газетирі")
    if rename.katottg is not None:
        matched = [p for p in positions if entities[p].katottg == rename.katottg]
        if len(matched) != 1:
            _fail(number, "код katottg не знайдено серед сутностей з новою назвою")
        return matched[0]
    for entity_type in RENAMED_TYPE_RANK:
        of_type = [p for p in positions if entities[p].type == entity_type]
        if len(of_type) == 1:
            return of_type[0]
        if of_type:
            _fail(number, "кілька сутностей одного типу з новою назвою, задай katottg")
    _fail(number, "нової назви немає в газетирі")


def apply_renames(
    entities: list[Entity],
    renames: Sequence[Rename],
    guard: NameGuard,
    metrics: Counter[str],
) -> list[Entity]:
    """Копія `entities`, де кожна пара `renames` додала запис у `names.former[]` своєї сутності.

    Сутності без змін повертаються тими самими об'єктами; порядок збережено.
    """
    index = _candidate_index(entities)
    current = list(entities)
    for number, rename in enumerate(renames, start=1):
        position = _pick_target(number, rename, entities, index.get(rename.new, []))
        target = current[position]
        guard.check(rename.old, target.id)
        record: dict[str, Any] = {"name": rename.old, "kind": rename.kind, "until": rename.date}
        names = {**target.names, "former": [*target.names["former"], record]}
        current[position] = dataclasses.replace(target, names=names)
        metrics["former_names_total"] += 1
    return current
