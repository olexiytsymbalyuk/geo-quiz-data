"""Перевірки газетира для воріт: Крим, КАТОТТГ, лічильники, літери ланцюжка.

Правило 2.1: у текст порушення потрапляє лише id сутності (чи порядковий номер), ніколи назва.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pipeline.gazetteer import GazetteerError, effective_chain_tier
from pipeline.katottg import KatottgUnit
from pipeline.letters import ALPHABET
from pipeline.policy import Rename
from tests.gates.gate_support import Gazetteer, TileFeature, safe_id

SCOPE = "ukraine"
COUNTRY_UA = "UA"
ARK_NAME = "Автономна Республіка Крим"
CRIMEA_CITY_NAMES = ("Сімферополь", "Севастополь", "Керч", "Ялта", "Євпаторія", "Джанкой", "Феодосія")
SEVASTOPOL_NAME = "Севастополь"
CRIMEA_CODE_PREFIXES = ("UA01", "UA85")
K_CODE_PREFIXES = ("UA80", "UA85")
ARK_CODE_PREFIX = "UA01"
SOFT_SIGN = chr(0x044C)
# Старшинство типів, з яких береться тип перейменованої сутності (серед однойменних пунктів із назвою `new`).
RENAMED_TYPE_RANK = ("city", "selyshche", "selo")

# Додаток Б: 27 одиниць O/K, 136 районів, 1 469 громад (без Криму), 463 міста; допуск ± 2.
EXPECTED_COUNTS: Mapping[str, int] = {"oblast_plus_k": 27, "raion": 136, "hromada": 1469, "city": 463}
COUNT_TOLERANCE = 2
_WORD_SPLIT_RE = re.compile("[\\s‐-―-]+")
_ALPHABET = frozenset(ALPHABET)


def uk_name(row: Mapping[str, Any]) -> str:
    name = json.loads(row["names"]).get("uk")
    return name if isinstance(name, str) else ""


def _code(row: Mapping[str, Any]) -> str:
    code = row.get("katottg")
    return code if isinstance(code, str) else ""


def _tier(row: Mapping[str, Any]) -> int | None:
    tier = json.loads(row["tiers"]).get(SCOPE)
    return tier if isinstance(tier, int) else None


# --- Крим ----------------------------------------------------------------------------------------------------------

def crimea_violations(gazetteer: Gazetteer) -> list[str]:
    """Усе кримське — з країною UA; сім міст знайдені й мають ланцюжок `parents` до АР Крим або є К-одиницею Севастополя."""
    rows = gazetteer.entities
    ark_ids = {row["id"] for row in rows if row["type"] == "oblast" and uk_name(row) == ARK_NAME}
    found: list[str] = [] if ark_ids else ["сутність Автономної Республіки Крим не знайдена"]
    for index, row in enumerate(rows):
        if _code(row).startswith(CRIMEA_CODE_PREFIXES) and row["country"] != COUNTRY_UA:
            found.append(f"sqlite entity {safe_id(row['id'], index)}: кримська сутність не з країною UA")
    for number, name in enumerate(CRIMEA_CITY_NAMES, start=1):
        cities = [r for r in rows if r["type"] == "city" and _code(r).startswith(CRIMEA_CODE_PREFIXES) and uk_name(r) == name]
        if len(cities) != 1:
            found.append(f"місто Криму №{number}: знайдено {len(cities)} сутностей, очікується одна")
        for city in cities:
            if not _chain_ends_in_crimea(city, ark_ids):
                found.append(f"sqlite entity {safe_id(city['id'], number)}: ланцюжок parents не закінчується на АР Крим чи Севастополь")
    return found


def _chain_ends_in_crimea(city: Mapping[str, Any], ark_ids: set[Any]) -> bool:
    parents = json.loads(city["parents"])
    if parents:
        return bool(parents[-1] in ark_ids)
    return uk_name(city) == SEVASTOPOL_NAME and _code(city).startswith("UA85")  # сама К-одиниця «Севастополь»


# --- КАТОТТГ -------------------------------------------------------------------------------------------------------

def katottg_name_violations(gazetteer: Gazetteer, units: Iterable[KatottgUnit]) -> tuple[list[str], int]:
    """(порушення, перевірено сутностей): `names.uk` кожної сутності з кодом дослівно дорівнює назві з кодифікатора."""
    by_code = {unit.code: unit.name for unit in units}
    found: list[str] = []
    checked = 0
    for index, row in enumerate(gazetteer.entities):
        code = _code(row)
        if not code:
            continue
        checked += 1
        if code not in by_code:
            found.append(f"sqlite entity {safe_id(row['id'], index)}: коду немає в КАТОТТГ")
        elif uk_name(row) != by_code[code]:
            found.append(f"sqlite entity {safe_id(row['id'], index)}: назва не дорівнює назві з КАТОТТГ")
    return found, checked


def _primary_type(holders: Sequence[Mapping[str, Any]]) -> str:
    """Тип перейменованої сутності: найстаршого рангу серед сутностей, канонічна назва яких дорівнює `new`."""
    types = {holder["type"] for holder in holders}
    return next((kind for kind in RENAMED_TYPE_RANK if kind in types), sorted(types)[0])


def rename_violations(renames: Sequence[Rename], gazetteer: Gazetteer, tile_features: Iterable[TileFeature]) -> list[str]:
    """Нова назва є в газетирі; стара не є назвою сутності того самого типу й не трапляється як `name:uk` у тайлах.

    Відхилення від брифу («стара не є канонічною назвою жодної сутності»): у КАТОТТГ лишаються однойменні
    неперейменовані пункти іншого типу (села «Красний Лиман», «Красний Луч», «Ватутіне», «Южне»). Тип перейменованої
    сутності береться з сутності з назвою `new`; сутності інших типів отримують виняток, лише якщо мають код КАТОТТГ.
    Фіча тайла (будь-який шар із `name:uk`) отримує виняток, лише якщо її `id` веде до такої сутності газетира.
    """
    rows = gazetteer.entities
    holders: dict[str, list[Mapping[str, Any]]] = {}
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        holders.setdefault(uk_name(row), []).append(row)
        by_id[row["id"]] = row
    old_in_tiles: dict[str, list[TileFeature]] = {}
    wanted_old = {rename.old for rename in renames}
    for feature in tile_features:
        name = feature.properties.get("name:uk")
        if isinstance(name, str) and name in wanted_old:
            old_in_tiles.setdefault(name, []).append(feature)
    found: list[str] = []
    for number, rename in enumerate(renames, start=1):
        new_holders = holders.get(rename.new, [])
        if not new_holders:
            found.append(f"перейменування №{number}: нової назви немає в газетирі")
        renamed_type = _primary_type(new_holders) if new_holders else None

        def excused(row: Mapping[str, Any] | None, renamed_type: str | None = renamed_type) -> bool:
            return row is not None and renamed_type is not None and row["type"] != renamed_type and bool(_code(row))

        if any(not excused(row) for row in holders.get(rename.old, [])):
            found.append(f"перейменування №{number}: стара назва є назвою сутності того самого типу")
        for feature in old_in_tiles.get(rename.old, []):
            if not excused(by_id.get(feature.properties.get("id"))):
                found.append(f"перейменування №{number}: стара назва в тайлах {feature.where()}")
                break
    return found


# --- лічильники ----------------------------------------------------------------------------------------------------

def entity_counts(gazetteer: Gazetteer) -> dict[str, int]:
    """O+K, P, H без Криму, M (усі `city`, K теж)."""
    counts = {key: 0 for key in EXPECTED_COUNTS}
    for row in gazetteer.entities:
        kind, code = row["type"], _code(row)
        if kind == "oblast" or (kind == "city" and code.startswith(K_CODE_PREFIXES)):
            counts["oblast_plus_k"] += 1
        if kind == "raion":
            counts["raion"] += 1
        if kind == "hromada" and not code.startswith(ARK_CODE_PREFIX):
            counts["hromada"] += 1
        if kind == "city":
            counts["city"] += 1
    return counts


# --- літери ланцюжка -----------------------------------------------------------------------------------------------

def parse_startable(gazetteer: Gazetteer) -> dict[int, frozenset[str]]:
    if len(gazetteer.meta) != 1:
        raise AssertionError(f"таблиця meta має один рядок, а не {len(gazetteer.meta)}")
    raw: Mapping[str, Sequence[str]] = json.loads(gazetteer.meta[0]["startable_json"])
    return {int(tier): frozenset(letters) for tier, letters in raw.items()}


def first_letter(name: str) -> str | None:
    """Перша літера алфавіту в першому слові назви (нижній регістр, NFC)."""
    words = [w for w in _WORD_SPLIT_RE.split(unicodedata.normalize("NFC", name).lower()) if w]
    letters = [c for c in words[0] if c in _ALPHABET] if words else []
    return letters[0] if letters else None


def soft_sign_violations(startable: Mapping[int, frozenset[str]]) -> list[str]:
    return [f"startable[{tier}]: містить «ь»" for tier, letters in sorted(startable.items()) if SOFT_SIGN in letters]


def starting_letter_violations(
    gazetteer: Gazetteer, startable: Mapping[int, frozenset[str]], minimum: int
) -> list[str]:
    """Кожна стартова літера яруса t має ≥ `minimum` назв серед населених пунктів ярусів ≤ t («ь» ловить інша перевірка)."""
    tiered = [(tier, first_letter(uk_name(row))) for row in gazetteer.entities if (tier := _tier(row)) is not None]
    found: list[str] = []
    for tier, letters in sorted(startable.items()):
        counts: dict[str, int] = {}
        for entity_tier, letter in tiered:
            if letter is not None and entity_tier <= tier:
                counts[letter] = counts.get(letter, 0) + 1
        found += [
            f"startable[{tier}]: літера №{ALPHABET.index(letter) + 1} має {counts.get(letter, 0)} назв, потрібно {minimum}"
            for letter in sorted(letters - {SOFT_SIGN})
            if counts.get(letter, 0) < minimum
        ]
    return found


def chain_letter_violations(gazetteer: Gazetteer, startable: Mapping[int, frozenset[str]]) -> tuple[list[str], int]:
    """(порушення, перевірено сутностей): `chain_letter` ∈ `startable[effective_chain_tier(ярус)]`."""
    found: list[str] = []
    checked = 0
    for index, row in enumerate(gazetteer.entities):
        tier = _tier(row)
        if tier is None:
            continue
        checked += 1
        where = f"sqlite entity {safe_id(row['id'], index)}"
        try:
            pool = startable[effective_chain_tier(tier, startable)]
        except GazetteerError:
            found.append(f"{where}: немає непорожнього пулу стартових літер для яруса")
            continue
        if row["chain_letter"] not in pool:
            found.append(f"{where}: chain_letter поза ефективним пулом")
    return found, checked
