"""Літери ланцюжка гри «Міста» (Додаток А.1, А.4 брифу).

Літера береться з канонічної назви як є (нижній регістр): ґ лишається ґ, без зведення до г.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

# А.1: 33 літери українського алфавіту
ALPHABET: tuple[str, ...] = tuple("абвгґдеєжзиіїйклмнопрстуфхцчшщьюя")
_ALPHABET_SET = frozenset(ALPHABET)

# Літери, які завжди пропускаються при відкаті (А.4, п. 2)
ALWAYS_SKIP: frozenset[str] = frozenset("ьйи")

# Розділювачі слів: пробіли, ASCII-дефіс і U+2010–2015
_WORD_SPLIT_RE = re.compile("[\\s\u2010-\u2015-]+")


def _letters(word: str) -> list[str]:
    """Лише літери ALPHABET: апостроф (будь-який), цифри, латиниця відкидаються."""
    return [c for c in word if c in _ALPHABET_SET]


def _words(name: str) -> list[str]:
    return [w for w in _WORD_SPLIT_RE.split(unicodedata.normalize("NFC", name).lower()) if w]


def compute_startable(names: Iterable[str], min_count: int) -> frozenset[str]:
    """Стартові літери: з кожної починається ≥ `min_count` назв; «ь» — ніколи."""
    counts: dict[str, int] = {}
    for name in names:
        words = _words(name)
        letters = _letters(words[0]) if words else []
        if letters:
            counts[letters[0]] = counts.get(letters[0], 0) + 1
    return frozenset(c for c, n in counts.items() if n >= min_count and c != "ь")


def chain_letter(name: str, startable: frozenset[str]) -> str:
    """Потрібна літера наступного ходу за канонічною назвою (А.4).

    Останнє слово → літери ALPHABET → з кінця, пропускаючи ALWAYS_SKIP і не-стартові;
    якщо слово вичерпане — його перша літера. `ValueError`, якщо літер немає.
    """
    words = _words(name)
    letters = _letters(words[-1]) if words else []
    if not letters:
        raise ValueError(f"у назві {name!r} немає літер алфавіту")
    for letter in reversed(letters):
        if letter not in ALWAYS_SKIP and letter in startable:
            return letter
    return letters[0]
