"""Парсер КАТОТТГ: структура фікстури повторює реальний `kodifikator-07-07.xlsx`."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from pipeline import katottg
from pipeline.katottg import KatottgError, KatottgUnit, counts, parse_katottg

# Заголовки точно як у реальному файлі (апостроф у «Категорія об’єкта» — U+2019).
HEADER = (
    "Перший рівень",
    "Другий рівень",
    "Третій рівень",
    "Четвертий рівень",
    "Додатковий рівень",
    "Категорія об’єкта",
    "Назва об’єкта",
)

O = "UA01000000000013043"
P = "UA01020000000022387"
H = "UA01020010000048857"
M = "UA01020050010045207"
C = "UA01020010010075540"
KYIV = "UA80000000000093317"
B = "UA80000000000126643"

Row = tuple[str | None, ...]

ROWS: list[Row] = [
    (O, None, None, None, None, "O", "Автономна Республіка Крим"),
    (O, P, None, None, None, "P", "Бахчисарайський"),
    (O, P, H, None, None, "H", "Андріївська "),  # хвостовий пробіл, як у файлі
    (O, P, H, C, None, "C", "Андріївка"),
    (O, P, "UA01020050000056842", M, None, "M", "Нова  Каховка"),  # подвійний пробіл
    (KYIV, KYIV, KYIV, KYIV, B, "B", "Голосіївський"),
]


def make_xlsx(path: Path, rows: list[Row], *, trailing_blank: bool = True) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Кодифікатор"
    ws.cell(row=1, column=6, value="ЗАТВЕРДЖЕНО\nнаказом Міністерства")
    ws.cell(row=3, column=1, value="Кодифікатор адміністративно-територіальних одиниць")
    for col, title in enumerate(HEADER, start=1):
        ws.cell(row=4, column=col, value=title)
    for r, row in enumerate(rows, start=5):
        for col, value in enumerate(row, start=1):
            ws.cell(row=r, column=col, value=value)
    if trailing_blank:
        ws.cell(row=5 + len(rows), column=1)  # порожній хвостовий рядок, як у реальному файлі
    wb.save(path)
    return path


def test_parents_and_categories(tmp_path: Path) -> None:
    units = parse_katottg(make_xlsx(tmp_path / "k.xlsx", ROWS))
    assert [u.category for u in units] == ["O", "P", "H", "C", "M", "B"]
    by_cat = {u.category: u for u in units}
    assert by_cat["O"] == KatottgUnit(O, "O", "Автономна Республіка Крим", ())
    assert by_cat["P"].parents == (O,)
    assert by_cat["H"].parents == (O, P)
    assert by_cat["C"].code == C
    assert by_cat["C"].parents == (O, P, H)
    assert by_cat["M"].parents == (O, P, "UA01020050000056842")
    # Київ: рівні 1-4 повторюють код міста; B — район (додатковий рівень), батько — лише місто.
    assert by_cat["B"].code == B
    assert by_cat["B"].parents == (KYIV,)


def test_k_has_no_parents(tmp_path: Path) -> None:
    rows: list[Row] = [(KYIV, None, None, None, None, "K", "Київ")]
    (unit,) = parse_katottg(make_xlsx(tmp_path / "k.xlsx", rows))
    assert unit == KatottgUnit(KYIV, "K", "Київ", ())


def test_name_is_stripped_and_squeezed(tmp_path: Path) -> None:
    units = parse_katottg(make_xlsx(tmp_path / "k.xlsx", ROWS))
    names = {u.category: u.name for u in units}
    assert names["H"] == "Андріївська"
    assert names["M"] == "Нова Каховка"


def test_name_case_is_preserved(tmp_path: Path) -> None:
    rows: list[Row] = [(O, None, None, None, None, "O", "ДОНЕЦЬКА")]
    (unit,) = parse_katottg(make_xlsx(tmp_path / "k.xlsx", rows))
    assert unit.name == "ДОНЕЦЬКА"


def test_trailing_blank_row_is_ignored(tmp_path: Path) -> None:
    units = parse_katottg(make_xlsx(tmp_path / "k.xlsx", ROWS, trailing_blank=True))
    assert len(units) == len(ROWS)


@pytest.mark.parametrize("category", ["T", "Z", "o", "", None])
def test_unknown_or_missing_category_raises_with_row(tmp_path: Path, category: str | None) -> None:
    rows: list[Row] = [ROWS[0], (O, P, None, None, None, category, "Щось")]
    with pytest.raises(KatottgError, match=r"рядок 6"):
        parse_katottg(make_xlsx(tmp_path / "k.xlsx", rows))


def test_malformed_code_raises(tmp_path: Path) -> None:
    rows: list[Row] = [("UA123", None, None, None, None, "O", "Область")]
    with pytest.raises(KatottgError, match=r"рядок 5"):
        parse_katottg(make_xlsx(tmp_path / "k.xlsx", rows))


def test_gap_in_levels_raises(tmp_path: Path) -> None:
    rows: list[Row] = [(O, None, H, None, None, "H", "Громада")]
    with pytest.raises(KatottgError, match=r"рядок 5"):
        parse_katottg(make_xlsx(tmp_path / "k.xlsx", rows))


def test_empty_name_raises(tmp_path: Path) -> None:
    rows: list[Row] = [(O, None, None, None, None, "O", "  ")]
    with pytest.raises(KatottgError, match=r"рядок 5"):
        parse_katottg(make_xlsx(tmp_path / "k.xlsx", rows))


def test_duplicate_code_raises(tmp_path: Path) -> None:
    rows: list[Row] = [ROWS[0], ROWS[0]]
    with pytest.raises(KatottgError, match=r"рядок 6"):
        parse_katottg(make_xlsx(tmp_path / "k.xlsx", rows))


def test_missing_header_raises(tmp_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.cell(row=1, column=1, value="нічого")
    wb.save(tmp_path / "k.xlsx")
    with pytest.raises(KatottgError, match="заголов"):
        parse_katottg(tmp_path / "k.xlsx")


def test_counts(tmp_path: Path) -> None:
    units = parse_katottg(make_xlsx(tmp_path / "k.xlsx", ROWS))
    assert counts(units) == {"O": 1, "P": 1, "H": 1, "M": 1, "C": 1, "B": 1}
    assert counts([]) == {}


def test_uppercase_categories_report() -> None:
    units = [
        KatottgUnit(O, "O", "ДОНЕЦЬКА", ()),
        KatottgUnit(P, "P", "Бахчисарайський", (O,)),
        KatottgUnit(H, "H", "ЯР", (O, P)),
    ]
    assert katottg.uppercase_categories(units) == {"O": 1, "H": 1}


def test_report_writes_json(tmp_path: Path) -> None:
    xlsx = make_xlsx(tmp_path / "k.xlsx", ROWS)
    out = tmp_path / "counts.json"
    report = katottg.build_report(xlsx, out)
    assert json.loads(out.read_text(encoding="utf-8")) == report
    assert report["counts"] == {"O": 1, "P": 1, "H": 1, "M": 1, "C": 1, "B": 1}
    assert report["total"] == 6
    assert report["uppercase_categories"] == {}
