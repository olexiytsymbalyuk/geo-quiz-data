"""Політика топонімів (правило 2.1): читання `config/policy.yaml`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pipeline.paths import CONFIG


RENAME_KINDS = frozenset({"soviet", "imperial", "historical"})
_KATOTTG = re.compile(r"UA\d{17}")


@dataclass(frozen=True)
class Rename:
    """Перейменування (декомунізація/деколонізація): стара форма стає лише `former[]`."""

    old: str
    new: str
    date: str  # ISO, дата ухвалення постанови
    source: str  # постанова ВРУ: номер і дата
    kind: str = "historical"  # soviet | imperial | historical (RENAME_KINDS)
    katottg: str | None = None  # код КАТОТТГ перейменованої сутності, коли однієї назви замало


@dataclass(frozen=True)
class Policy:
    neutral_countries: frozenset[str]
    named_neighbours: frozenset[str]
    exonym_blocklist: tuple[str, ...]
    renames_check: tuple[Rename, ...]
    renames_homonym_exceptions: frozenset[str]  # коди КАТОТТГ неперейменованих однойменних сутностей (R42)
    startable_min: int
    blocked_sentinel: str


def _require(raw: dict[str, Any], key: str) -> Any:
    if key not in raw or raw[key] is None:
        raise ValueError(f"policy.yaml: немає ключа {key!r}")
    return raw[key]


def _strings(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise ValueError(f"policy.yaml: {key!r} має бути списком непорожніх рядків")
    return list(value)


def _renames(items: Any) -> tuple[Rename, ...]:
    renames: list[Rename] = []
    for number, item in enumerate(items, start=1):
        try:
            katottg = item.get("katottg")
            rename = Rename(
                old=str(item["old"]),
                new=str(item["new"]),
                date=str(item["date"]),
                source=str(item["source"]),
                kind=str(item.get("kind", "historical")),
                katottg=None if katottg is None else str(katottg),
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"policy.yaml: некоректний запис renames_check №{number}") from exc
        if rename.kind not in RENAME_KINDS:
            raise ValueError(f"policy.yaml: renames_check №{number}: kind поза {sorted(RENAME_KINDS)}")
        if rename.katottg is not None and not _KATOTTG.fullmatch(rename.katottg):
            raise ValueError(f"policy.yaml: renames_check №{number}: katottg не за шаблоном UA + 17 цифр")
        renames.append(rename)
    return tuple(renames)


def _homonym_exceptions(items: Any, old_names: set[str]) -> frozenset[str]:
    if not isinstance(items, list):
        raise ValueError("policy.yaml: 'renames_homonym_exceptions' має бути списком")
    codes: set[str] = set()
    for number, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("katottg"), str) or not isinstance(item.get("name"), str):
            raise ValueError(f"policy.yaml: некоректний запис renames_homonym_exceptions №{number}")
        if not _KATOTTG.fullmatch(item["katottg"]):
            raise ValueError(f"policy.yaml: renames_homonym_exceptions №{number}: katottg не за шаблоном UA + 17 цифр")
        if item["name"] not in old_names:
            raise ValueError(f"policy.yaml: renames_homonym_exceptions №{number}: name не збігається з old жодної пари")
        if item["katottg"] in codes:
            raise ValueError(f"policy.yaml: renames_homonym_exceptions №{number}: повторний katottg")
        codes.add(item["katottg"])
    return frozenset(codes)


def load_policy(path: Path = CONFIG / "policy.yaml") -> Policy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("policy.yaml: очікується відображення верхнього рівня")

    renames = _renames(_require(raw, "renames_check"))
    exceptions = _homonym_exceptions(_require(raw, "renames_homonym_exceptions"), {r.old for r in renames})

    startable_min = _require(raw, "startable_min")
    if isinstance(startable_min, bool) or not isinstance(startable_min, int):
        raise ValueError("policy.yaml: 'startable_min' має бути цілим числом")
    sentinel = _require(raw, "blocked_sentinel")
    if not isinstance(sentinel, str):
        raise ValueError("policy.yaml: 'blocked_sentinel' має бути рядком")

    return Policy(
        neutral_countries=frozenset(_strings(_require(raw, "neutral_countries"), "neutral_countries")),
        named_neighbours=frozenset(_strings(_require(raw, "named_neighbours"), "named_neighbours")),
        exonym_blocklist=tuple(_strings(_require(raw, "exonym_blocklist"), "exonym_blocklist")),
        renames_check=renames,
        renames_homonym_exceptions=exceptions,
        startable_min=startable_min,
        blocked_sentinel=sentinel,
    )
