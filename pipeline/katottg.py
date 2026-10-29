"""Парсер КАТОТТГ — істина для назв, категорій і ієрархії одиниць України.

Джерело: `kodifikator-07-07.xlsx` (кодифікатор, наказ Мінрозвитку № 1321 від 07.07.2026).
Аркуш має кілька службових рядків, потім заголовок (п'ять рівнів, категорія, назва) і дані:
кожен рядок — одна одиниця, у колонках рівнів стоять коди предків і код самої одиниці
(найглибший заповнений рівень). Назви беруться дослівно: лише `strip()` і стиснення пробілів;
регістр тут не виправляється.

Запуск: `python -m pipeline.katottg --report` -> `data/work/katottg-counts.json`.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import openpyxl
import yaml

from pipeline.fetch import LOCK, sha256_of
from pipeline.paths import RAW, WORK

Category = Literal["O", "K", "P", "H", "M", "X", "C", "B"]
CATEGORIES: tuple[Category, ...] = ("O", "K", "P", "H", "M", "X", "C", "B")

_LEVEL_TITLES = ("Перший рівень", "Другий рівень", "Третій рівень", "Четвертий рівень", "Додатковий рівень")
_CODE_RE = re.compile(r"UA\d{17}")
_HEADER_SCAN_ROWS = 20
KATOTTG_FILE = RAW / "katottg" / "kodifikator-07-07.xlsx"
COUNTS_FILE = WORK / "katottg-counts.json"


class KatottgError(ValueError):
    """Рядок кодифікатора не відповідає очікуваній структурі (повідомлення містить номер рядка)."""


@dataclass(frozen=True)
class KatottgUnit:
    code: str  # `UA` + 17 цифр
    category: Category
    name: str
    parents: tuple[str, ...]  # коди предків від області вниз, без самої одиниці


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _find_columns(header: tuple[object, ...]) -> tuple[list[int], int, int] | None:
    titles = [_text(c) for c in header]
    if not all(t in titles for t in _LEVEL_TITLES):
        return None
    levels = [titles.index(t) for t in _LEVEL_TITLES]
    category = next((i for i, t in enumerate(titles) if t.startswith("Категорія")), None)
    name = next((i for i, t in enumerate(titles) if t.startswith("Назва")), None)
    if category is None or name is None:
        return None
    return levels, category, name


def _cell(row: tuple[object, ...], index: int) -> object:
    return row[index] if index < len(row) else None


def _parse_row(
    number: int, row: tuple[object, ...], levels: list[int], cat_col: int, name_col: int
) -> KatottgUnit:
    codes = [_text(_cell(row, i)) for i in levels]
    filled = [c for c in codes if c]
    if not filled:
        raise KatottgError(f"рядок {number}: немає жодного коду")
    if any(not codes[i] and codes[i + 1] for i in range(len(codes) - 1)):
        raise KatottgError(f"рядок {number}: пропущений проміжний рівень")
    for code in filled:
        if not _CODE_RE.fullmatch(code):
            raise KatottgError(f"рядок {number}: код {code!r} не має вигляду UA + 17 цифр")
    category = _text(_cell(row, cat_col))
    if category not in CATEGORIES:
        raise KatottgError(f"рядок {number}: категорія {category!r} поза переліком {''.join(CATEGORIES)}")
    name = " ".join(_text(_cell(row, name_col)).split())
    if not name:
        raise KatottgError(f"рядок {number}: порожня назва")
    own = filled[-1]
    # Київ і Севастополь повторюють власний код на рівнях 1-4: зайве прибираємо, порядок зберігаємо.
    parents = tuple(dict.fromkeys(c for c in filled[:-1] if c != own))
    return KatottgUnit(own, category, name, parents)


def parse_katottg(path: Path) -> list[KatottgUnit]:
    """Усі одиниці кодифікатора в порядку файлу; порожні рядки пропускаються."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        columns: tuple[list[int], int, int] | None = None
        units: list[KatottgUnit] = []
        seen: set[str] = set()
        for number, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if columns is None:
                if number > _HEADER_SCAN_ROWS:
                    break
                columns = _find_columns(row)
                continue
            if not any(_text(c) for c in row):
                continue
            unit = _parse_row(number, row, *columns)
            if unit.code in seen:
                raise KatottgError(f"рядок {number}: код {unit.code} повторюється")
            seen.add(unit.code)
            units.append(unit)
    finally:
        wb.close()
    if columns is None:
        raise KatottgError(f"{path.name}: не знайдено рядка заголовка серед перших {_HEADER_SCAN_ROWS}")
    return units


def counts(units: Iterable[KatottgUnit]) -> dict[str, int]:
    """Кількість одиниць за категоріями (лише наявні), у порядку O,K,P,H,M,X,C,B."""
    c = Counter(u.category for u in units)
    return {cat: c[cat] for cat in CATEGORIES if c[cat]}


def uppercase_categories(units: Iterable[KatottgUnit]) -> dict[str, int]:
    """Скільки назв у кожній категорії записано повністю ВЕЛИКИМИ (їх тут не виправляємо)."""
    c = Counter(u.category for u in units if sum(ch.isalpha() for ch in u.name) >= 2 and u.name.isupper())
    return {cat: c[cat] for cat in CATEGORIES if c[cat]}


def build_report(xlsx: Path, out: Path) -> dict[str, Any]:
    """Розбирає файл і пише `counts`-звіт у `out`."""
    units = parse_katottg(xlsx)
    report: dict[str, Any] = {
        "counts": counts(units),
        "total": len(units),
        "uppercase_categories": uppercase_categories(units),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _verify_locked(path: Path) -> None:
    """Файл з кешу мусить збігатися з sha256 у `config/sources.lock`."""
    lock = yaml.safe_load(LOCK.read_text(encoding="utf-8"))
    expected = lock["sources"]["katottg"]["files"][path.name]["sha256"]
    actual = sha256_of(path)
    if actual != expected:
        raise KatottgError(f"{path.name}: sha256 {actual} не збігається з lock {expected}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["--report"]:
        sys.stderr.write("використання: python -m pipeline.katottg --report\n")
        return 2
    _verify_locked(KATOTTG_FILE)
    report = build_report(KATOTTG_FILE, COUNTS_FILE)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
