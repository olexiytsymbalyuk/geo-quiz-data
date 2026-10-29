"""Ключ нормалізації назв (§5.8) і сторож заборонених літер."""

from __future__ import annotations

import pytest

from pipeline.text import FORBIDDEN_RE, has_forbidden, key_without_apostrophes, normalize_key


@pytest.mark.parametrize(
    "raw",
    ["Кам'янець-Подільський", "Кам\u2019янець Подільський", "  КАМ\u02bcЯНЕЦЬ-ПОДІЛЬСЬКИЙ "],
)
def test_apostrophes_and_hyphens_share_key(raw: str) -> None:
    assert normalize_key(raw) == "кам\u02bcянець подільський"


def test_mixed_script_twins() -> None:  # латинська K у кириличному слові
    assert normalize_key("\u004bиїв") == normalize_key("Київ")


def test_pure_latin_untouched() -> None:
    assert normalize_key("Kyiv") == "kyiv"


def test_g_with_upturn() -> None:
    assert normalize_key("Ґорґани") == normalize_key("Горгани")


def test_forbidden_rejected() -> None:
    assert normalize_key("Харків") is not None
    assert normalize_key("аб\u044bв") is None  # синтетичний рядок, не топонім
    assert has_forbidden("\u0401") and not has_forbidden("Їжівці")


def test_without_apostrophes() -> None:
    assert key_without_apostrophes("кам\u02bcянець подільський") == "камянець подільський"


def test_empty_and_blank() -> None:
    assert normalize_key("") == ""
    assert normalize_key("   \t ") == ""


@pytest.mark.parametrize(
    "ch", ["\u0401", "\u0451", "\u042b", "\u044b", "\u042a", "\u044a", "\u042d", "\u044d"]
)
def test_every_forbidden_letter_detected(ch: str) -> None:
    assert FORBIDDEN_RE.search(ch)
    assert normalize_key(f"аб{ch}в") is None


def test_forbidden_not_masked_by_twins() -> None:
    # слово змішаного письма із забороненою літерою все одно відхиляється
    assert normalize_key("\u004bи\u044bв") is None


def test_pure_latin_twin_letters_not_replaced() -> None:
    assert normalize_key("Copenhagen") == "copenhagen"


def test_dash_variants_equal_space() -> None:
    assert normalize_key("Івано\u2013Франківськ") == "івано франківськ"
    assert normalize_key("Івано\u2212Франківськ") == "івано франківськ"
    assert normalize_key("Івано - Франківськ") == "івано франківськ"
