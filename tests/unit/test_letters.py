"""Літери ланцюжка гри «Міста» (Додаток А.1, А.4)."""

from __future__ import annotations

import pytest

from pipeline.letters import ALPHABET, ALWAYS_SKIP, chain_letter, compute_startable

S = frozenset(ALPHABET) - {"ь", "ї", "ґ", "и", "й"}


def test_alphabet_has_33_letters() -> None:
    assert len(ALPHABET) == 33
    assert len(set(ALPHABET)) == 33
    assert ALPHABET[0] == "а" and ALPHABET[-1] == "я"
    assert ALWAYS_SKIP == frozenset("ьйи")


@pytest.mark.parametrize(
    ("name", "letter"),
    [
        ("Київ", "в"),
        ("Відень", "н"),
        ("Хмельницький", "к"),
        ("Умань", "н"),
        ("Чернівці", "і"),
        ("Броди", "д"),
        ("Гаї", "а"),
        ("Дубай", "а"),
        ("Мюнхен", "н"),
        ("Лісабон", "н"),
        ("Кривий Ріг", "г"),
        ("Біла Церква", "а"),
        ("Кам'янець-Подільський", "к"),
    ],
)
def test_chain_letter_table(name: str, letter: str) -> None:
    assert chain_letter(name, S) == letter


@pytest.mark.parametrize("apostrophe", ["'", "’", "ʼ"])
def test_apostrophe_variants(apostrophe: str) -> None:
    assert chain_letter(f"Кам{apostrophe}янець-Подільський", S) == "к"
    assert chain_letter(f"Рив{apostrophe}я", S) == "я"


@pytest.mark.parametrize("hyphen", ["-", "‐", "‑", "‒", "–", "—", "―"])
def test_hyphen_variants(hyphen: str) -> None:
    assert chain_letter(f"Кам'янець{hyphen}Подільський", S) == "к"
    assert chain_letter(f"Івано{hyphen}Франківськ", S) == "к"


def test_digits_and_latin_ignored() -> None:
    assert chain_letter("Київ2", S) == "в"
    assert chain_letter("KиївX", S) == "в"


def test_letter_is_taken_from_canonical_name_without_folding() -> None:
    # ґ лишається ґ (не зводиться до г), коли вона стартова
    startable = S | {"ґ"}
    assert chain_letter("Ґуляйполе", startable) == "е"
    assert chain_letter("Тиґр", startable) == "р"
    assert chain_letter("Тиґ", startable) == "ґ"
    assert chain_letter("ТИҐ", startable) == "ґ"
    # не стартова ґ -> крок ліворуч
    assert chain_letter("Тиґ", S) == "т"


def test_non_startable_steps_left() -> None:
    assert chain_letter("Гаї", S | {"ї"}) == "ї"
    assert chain_letter("Гаї", S) == "а"


def test_exhausted_word_returns_first_letter() -> None:
    assert chain_letter("Ьй", S) == "ь"
    assert chain_letter("Иии", S) == "и"
    assert chain_letter("Ий", frozenset()) == "и"
    assert chain_letter("Київ", frozenset()) == "к"


def test_last_word_only() -> None:
    assert chain_letter("Ьй Київ", S) == "в"
    assert chain_letter("Київ Ьй", S) == "ь"


def test_no_alphabet_letters_raises() -> None:
    for bad in ["12", "Місто 2", "'", "abc", "", "  - "]:
        with pytest.raises(ValueError):
            chain_letter(bad, S)


def test_startable_threshold() -> None:
    names = ["Ананьїв"] * 10 + ["Бар"] * 9 + ["Ьх"] * 20
    assert compute_startable(names, 10) == {"а"}


def test_startable_edges() -> None:
    names = ["Ананьїв"] * 10 + ["Бар"] * 9
    assert compute_startable(names, 9) == {"а", "б"}
    assert compute_startable(names, 11) == frozenset()
    assert isinstance(compute_startable(names, 10), frozenset)


def test_startable_first_word_lowercase_and_non_alphabet_ignored() -> None:
    names = ["Кривий Ріг"] * 3 + ["кривошиїнці"] * 2 + ["'Абв"] * 5 + ["9"] * 50 + ["Kyiv"] * 50
    assert compute_startable(names, 5) == {"к", "а"}
    assert compute_startable(["Ріг Кривий"] * 3, 3) == {"р"}


def test_startable_keeps_gge_distinct() -> None:
    assert compute_startable(["Ґава"] * 10, 10) == {"ґ"}
