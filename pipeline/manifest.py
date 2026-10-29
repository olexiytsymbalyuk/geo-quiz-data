"""Маніфест релізу `dist/<версія>/manifest.json`: версія, походження джерел, суми артефактів."""

from __future__ import annotations

import json
import re
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import yaml

from pipeline.fetch import sha256_of

MANIFEST_NAME = "manifest.json"
# З брифу; номер наказу звіряється в задачі 21. Дату перевіряємо проти last_modified xlsx у lock.
KATOTTG_ORDER = "№ 1321 від 07.07.2026"
_ORDER_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})$")
_NE_TAG_RE = re.compile(r"/v(\d+\.\d+\.\d+)/")


class ManifestError(RuntimeError):
    """Lock не містить того, що потрібно маніфесту, або суперечить сталим значенням."""


def _sources(lock: Path) -> dict[str, Any]:
    data = yaml.safe_load(lock.read_text(encoding="utf-8")) or {}
    sources: dict[str, Any] = data.get("sources", {})
    return sources


def _first_file(sources: dict[str, Any], source: str) -> dict[str, Any]:
    files: dict[str, Any] = sources.get(source, {}).get("files", {})
    if not files:
        raise ManifestError(f"у lock немає файлів джерела {source}")
    first: dict[str, Any] = next(iter(files.values()))
    return first


def ne_version_from_lock(lock: Path) -> str:
    """Версія Natural Earth — з тегу `vX.Y.Z` в URL файлів у lock (Ruling 8)."""
    url = str(_first_file(_sources(lock), "ne_admin0_ukr")["url"])
    match = _NE_TAG_RE.search(url)
    if match is None:
        raise ManifestError("в URL Natural Earth у lock немає тегу версії vX.Y.Z")
    return match.group(1)


def _order_date() -> date:
    match = _ORDER_DATE_RE.search(KATOTTG_ORDER)
    if match is None:
        raise ManifestError("KATOTTG_ORDER не закінчується датою ДД.ММ.РРРР")
    day, month, year = (int(part) for part in match.groups())
    return date(year, month, day)


def _check_katottg_order(sources: dict[str, Any]) -> None:
    modified = parsedate_to_datetime(str(_first_file(sources, "katottg")["last_modified"])).date()
    if modified != _order_date():
        raise ManifestError(f"дата наказу {_order_date()} не збігається з last_modified кодифікатора {modified}")


def _artifacts(dist: Path) -> dict[str, dict[str, Any]]:
    files = sorted(p for p in dist.iterdir() if p.is_file() and p.name != MANIFEST_NAME)
    return {p.name: {"sha256": sha256_of(p), "size": p.stat().st_size} for p in files}


def write_manifest(dist: Path, version: str, lock: Path, wd_date: str) -> Path:
    """Пише `manifest.json` у `dist`; `artifacts` — sha256 і розмір кожного файлу, крім самого маніфесту."""
    sources = _sources(lock)
    _check_katottg_order(sources)
    osm_timestamp = _first_file(sources, "osm_ukraine").get("osm_timestamp")
    if not osm_timestamp:
        raise ManifestError("у lock немає osm_timestamp для OSM")
    manifest = {
        "dataset_version": version,
        "sources": sources,
        "katottg_order": KATOTTG_ORDER,
        "ne_version": ne_version_from_lock(lock),
        "osm_timestamp": osm_timestamp,
        "wikidata_queried_at": wd_date,
        "artifacts": _artifacts(dist),
    }
    path = dist / MANIFEST_NAME
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
