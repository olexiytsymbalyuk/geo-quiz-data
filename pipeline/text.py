"""Ключ нормалізації назв (бриф §5.8) і сторож заборонених літер (правило 2.1).

Заборонені літери — лише escape-послідовностями: жодного гліфа в коді.
"""

from __future__ import annotations

import re
import unicodedata

# Чотири неукраїнські літери, обидва регістри: U+0401/0451, U+042B/044B, U+042A/044A, U+042D/044D.
FORBIDDEN_RE: re.Pattern[str] = re.compile("[\u0401\u0451\u042b\u044b\u042a\u044a\u042d\u044d]")

_APOSTROPHE = "\u02bc"  # канонічний апостроф ключа
# ' ’ ʼ ‘ ` ´ ʹ ′
_APOSTROPHES = "\u0027\u2019\u02bc\u2018\u0060\u00b4\u02b9\u2032"
_APOSTROPHE_TABLE = {ord(c): _APOSTROPHE for c in _APOSTROPHES}
# U+2010–2015 і U+2212 -> «-»
_HYPHEN_TABLE = {ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"}
# Латинські двійники a c e i o p x y k m t h -> кириличні відповідники.
_TWINS_TABLE = {
    ord("a"): "\u0430",
    ord("c"): "\u0441",
    ord("e"): "\u0435",
    ord("i"): "\u0456",
    ord("o"): "\u043e",
    ord("p"): "\u0440",
    ord("x"): "\u0445",
    ord("y"): "\u0443",
    ord("k"): "\u043a",
    ord("m"): "\u043c",
    ord("t"): "\u0442",
    ord("h"): "\u043d",
}
_CYRILLIC_RE = re.compile("[\u0400-\u04ff]")
_LATIN_RE = re.compile("[a-z]")


def has_forbidden(s: str) -> bool:
    """Чи містить рядок одну з чотирьох заборонених літер (перевірка на сирому NFC)."""
    return FORBIDDEN_RE.search(unicodedata.normalize("NFC", s)) is not None


def _fix_mixed_word(word: str) -> str:
    """Латинські двійники замінюються кириличними лише у слові змішаного письма."""
    if _CYRILLIC_RE.search(word) and _LATIN_RE.search(word):
        return word.translate(_TWINS_TABLE)
    return word


def normalize_key(s: str) -> str | None:
    """Ключ §5.8; `None`, якщо є заборонена літера (перевірка — до будь-яких замін)."""
    s = unicodedata.normalize("NFC", s)
    if FORBIDDEN_RE.search(s):
        return None
    s = s.lower()
    s = s.translate(_APOSTROPHE_TABLE).translate(_HYPHEN_TABLE)
    # дефіс ≡ пробіл; split() обрізає й стискає пробіли
    words = s.replace("-", " ").split()
    return " ".join(_fix_mixed_word(w) for w in words).replace("\u0491", "\u0433")  # ґ -> г


def key_without_apostrophes(key: str) -> str:
    """Другий ключ: нормалізований ключ без апострофів."""
    return key.replace(_APOSTROPHE, "")
