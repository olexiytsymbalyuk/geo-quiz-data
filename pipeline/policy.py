"""Політика топонімів (правило 2.1): читання `config/policy.yaml`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pipeline.paths import CONFIG


@dataclass(frozen=True)
class Rename:
    """Перейменування (декомунізація/деколонізація): стара форма стає лише `former[]`."""

    old: str
    new: str
    date: str  # ISO, дата ухвалення постанови
    source: str  # постанова ВРУ: номер і дата


@dataclass(frozen=True)
class Policy:
    neutral_countries: frozenset[str]
    named_neighbours: frozenset[str]
    exonym_blocklist: tuple[str, ...]
    renames_check: tuple[Rename, ...]
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


def load_policy(path: Path = CONFIG / "policy.yaml") -> Policy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("policy.yaml: очікується відображення верхнього рівня")

    renames: list[Rename] = []
    for item in _require(raw, "renames_check"):
        try:
            renames.append(
                Rename(
                    old=str(item["old"]),
                    new=str(item["new"]),
                    date=str(item["date"]),
                    source=str(item["source"]),
                )
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"policy.yaml: некоректний запис renames_check: {item!r}") from exc

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
        renames_check=tuple(renames),
        startable_min=startable_min,
        blocked_sentinel=sentinel,
    )
