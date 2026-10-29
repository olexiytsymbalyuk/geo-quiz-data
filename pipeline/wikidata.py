"""Населення, координати й обласні центри з Wikidata — для приєднання до газетира за кодом КАТОТТГ.

Два SPARQL-запити до WDQS (по одному на тип, `pipeline/queries/*.rq`) без жодних мовних міток:
назви беремо лише з КАТОТТГ. Ключ усюди — код КАТОТТГ з P9435 (лише обрізані пробіли по краях).
Обидві сирі відповіді разом із часом запиту лежать в одному кеші `data/raw/wikidata-<YYYY-MM-DD>.json`
(дата UTC); у `config/sources.lock` Wikidata не потрапляє. Без `refresh` найсвіжіший кеш читається
без мережі.

Запуск: `python -m pipeline.wikidata [--refresh]` -> `data/work/wikidata-report.json`.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import requests

from pipeline.fetch import USER_AGENT, _utc_now
from pipeline.paths import RAW, WORK

ENDPOINT = "https://query.wikidata.org/sparql"
QUERIES = Path(__file__).parent / "queries"
REPORT_FILE = WORK / "wikidata-report.json"
_TIMEOUT = (15, 120)  # (з'єднання, відповідь), секунди
_MAX_RETRIES = 2
_RETRY_PAUSE = 10.0  # секунди, якщо сервер не вказав Retry-After
_RETRY_PAUSE_MAX = 60.0
_CACHE_GLOB = "wikidata-????-??-??.json"

_QID_RE = re.compile(r"^http://www\.wikidata\.org/entity/(Q\d+)$")
_YEAR_RE = re.compile(r"^(\d{4})-")
_POINT_RE = re.compile(r"^Point\((-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?) (-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\)$")
_PREFERRED, _NORMAL = "PreferredRank", "NormalRank"


class WikidataError(RuntimeError):
    """Запит до WDQS не вдався, або відповідь/кеш мають неочікувану форму."""


@dataclass(frozen=True)
class WdItem:
    qid: str
    population: int | None
    population_year: int | None
    lat: float | None
    lon: float | None


@dataclass(frozen=True)
class WikidataResult:
    items: dict[str, WdItem]  # код КАТОТТГ -> об'єкт (без кодів з конфліктом)
    conflicts: dict[str, list[str]]  # код КАТОТТГ -> відсортовані QID (кілька об'єктів на один код)
    capitals: dict[str, str]  # код області -> код адміністративного центру (лише однозначні)
    queried_at: str  # ISO 8601 UTC
    cache_path: Path
    capital_conflicts: dict[str, list[str]] = field(default_factory=dict)  # область -> відсортовані коди центрів


def _bindings(sparql_json: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        rows = sparql_json["results"]["bindings"]
    except (KeyError, TypeError) as exc:
        raise WikidataError("відповідь SPARQL без results.bindings") from exc
    if not isinstance(rows, list):
        raise WikidataError("results.bindings має бути списком")
    return rows


def _value(row: dict[str, Any], name: str) -> str | None:
    cell = row.get(name)
    return None if cell is None else str(cell["value"])


def _required(row: dict[str, Any], name: str) -> str:
    value = _value(row, name)
    if value is None or not value.strip():
        raise WikidataError(f"у рядку відповіді немає обов'язкового поля {name}")
    return value.strip()


def _qid(uri: str) -> str:
    match = _QID_RE.fullmatch(uri)
    if match is None:
        raise WikidataError(f"{uri!r} не схожий на сутність Wikidata")
    return match.group(1)


def _parse_point(wkt: str | None) -> tuple[float, float] | None:
    """`Point(lon lat)` -> `(lat, lon)`; інше (інша планета, сміття, поза межами) -> `None`."""
    if wkt is None:
        return None
    match = _POINT_RE.fullmatch(wkt.strip())
    if match is None:
        return None
    lon, lat = float(match.group(1)), float(match.group(2))
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _population(row: dict[str, Any]) -> tuple[str, str, int] | None:
    """`(ранг, час P585, значення)` одного твердження про населення; `None`, якщо його немає."""
    raw = _value(row, "pop")
    if raw is None:
        return None
    try:
        number = int(Decimal(raw).to_integral_value())
    except InvalidOperation as exc:
        raise WikidataError(f"населення {raw!r} не є числом") from exc
    rank = (_value(row, "popRank") or "").rsplit("#", 1)[-1]
    return rank, _value(row, "popTime") or "", number


def _pick_population(candidates: set[tuple[str, str, int]]) -> tuple[int, int | None] | None:
    """Preferred, якщо є; інакше normal; серед них — найпізніший P585. Deprecated ігноруються."""
    for wanted in (_PREFERRED, _NORMAL):
        ranked = [c for c in candidates if c[0] == wanted]
        if ranked:
            _, moment, number = max(ranked, key=lambda c: (c[1], c[2]))
            year = _YEAR_RE.match(moment)
            return number, int(year.group(1)) if year else None
    return None


def parse_items(sparql_json: dict[str, Any]) -> tuple[dict[str, WdItem], dict[str, list[str]]]:
    """Відповідь `katottg_items.rq` -> `(items, conflicts)`.

    `items` — код КАТОТТГ -> `WdItem`; код, до якого належить кілька різних QID, потрапляє
    лише в `conflicts` (код -> відсортовані QID) і не потрапляє в `items`.
    """
    qids: dict[str, set[str]] = defaultdict(set)
    populations: dict[tuple[str, str], set[tuple[str, str, int]]] = defaultdict(set)
    points: dict[tuple[str, str], set[tuple[float, float]]] = defaultdict(set)
    for row in _bindings(sparql_json):
        code = _required(row, "code")
        qid = _qid(_required(row, "item"))
        qids[code].add(qid)
        key = (code, qid)
        if (pop := _population(row)) is not None:
            populations[key].add(pop)
        if (point := _parse_point(_value(row, "coord"))) is not None:
            points[key].add(point)

    items: dict[str, WdItem] = {}
    conflicts: dict[str, list[str]] = {}
    for code, found in qids.items():
        if len(found) > 1:
            conflicts[code] = sorted(found)
            continue
        (qid,) = found
        picked = _pick_population(populations.get((code, qid), set()))
        point = min(points[(code, qid)]) if points.get((code, qid)) else None
        items[code] = WdItem(
            qid,
            picked[0] if picked else None,
            picked[1] if picked else None,
            point[0] if point else None,
            point[1] if point else None,
        )
    return items, conflicts


def split_capitals(sparql_json: dict[str, Any]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Відповідь `oblast_capitals.rq` -> `(capitals, conflicts)`.

    `capitals` — код області -> код центру; область із кількома різними чинними центрами
    (напр. юридичний і фактичний) потрапляє лише в `conflicts` (область -> відсортовані коди).
    """
    found: dict[str, set[str]] = defaultdict(set)
    for row in _bindings(sparql_json):
        found[_required(row, "oblastCode")].add(_required(row, "centerCode"))
    capitals = {oblast: next(iter(c)) for oblast, c in found.items() if len(c) == 1}
    conflicts = {oblast: sorted(c) for oblast, c in found.items() if len(c) > 1}
    return capitals, conflicts


def parse_capitals(sparql_json: dict[str, Any]) -> dict[str, str]:
    """Код області -> код центру (лише області з одним чинним центром)."""
    return split_capitals(sparql_json)[0]


def _pause(response: requests.Response | None, attempt: int) -> float:
    retry_after = None if response is None else response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return min(float(retry_after), _RETRY_PAUSE_MAX)
        except ValueError:
            pass
    return _RETRY_PAUSE * (attempt + 1)


def _run_query(query_file: str) -> dict[str, Any]:
    """Один запит до WDQS; на 429/5xx і обриви з'єднання — до `_MAX_RETRIES` повторів із паузою."""
    query = (QUERIES / query_file).read_text(encoding="utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
    last_error = ""
    for attempt in range(_MAX_RETRIES + 1):
        response: requests.Response | None = None
        try:
            response = requests.get(
                ENDPOINT, params={"query": query, "format": "json"}, headers=headers, timeout=_TIMEOUT
            )
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
                return payload
            last_error = f"HTTP {response.status_code}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = type(exc).__name__
        except requests.HTTPError as exc:
            raise WikidataError(f"{query_file}: {exc}") from exc
        except ValueError as exc:
            raise WikidataError(f"{query_file}: відповідь WDQS не є JSON") from exc
        if attempt < _MAX_RETRIES:
            time.sleep(_pause(response, attempt))
    raise WikidataError(f"{query_file}: WDQS недоступний після {_MAX_RETRIES} повторів ({last_error})")


def _latest_cache(raw: Path) -> Path | None:
    return max(raw.glob(_CACHE_GLOB), default=None, key=lambda p: p.name) if raw.is_dir() else None


def _build_result(queried_at: str, items_json: dict[str, Any], capitals_json: dict[str, Any], path: Path) -> WikidataResult:
    items, conflicts = parse_items(items_json)
    capitals, capital_conflicts = split_capitals(capitals_json)
    return WikidataResult(items, conflicts, capitals, queried_at, path, capital_conflicts)


def _from_cache(path: Path) -> WikidataResult:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        queried_at = str(data["queried_at"])
        items_json, capitals_json = data["items"], data["capitals"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WikidataError(f"{path.name}: кеш пошкоджений; python -m pipeline.wikidata --refresh") from exc
    return _build_result(queried_at, items_json, capitals_json, path)


def fetch(raw: Path, refresh: bool = False) -> WikidataResult:
    """Найсвіжіший кеш `raw/wikidata-*.json` без мережі; інакше (або з `refresh`) два запити й новий кеш."""
    if not refresh and (cached := _latest_cache(raw)) is not None:
        return _from_cache(cached)
    queried_at = _utc_now()
    body: dict[str, Any] = {
        "queried_at": queried_at,
        "items": _run_query("katottg_items.rq"),
        "capitals": _run_query("oblast_capitals.rq"),
    }
    dest = raw / f"wikidata-{queried_at[:10]}.json"
    _build_result(queried_at, body["items"], body["capitals"], dest)  # розбір до запису: хибна відповідь кеш не псує
    part = dest.with_name(dest.name + ".part")
    raw.mkdir(parents=True, exist_ok=True)
    try:
        part.write_text(json.dumps(body, ensure_ascii=False) + "\n", encoding="utf-8")
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return _from_cache(dest)


def build_report(result: WikidataResult, out: Path) -> dict[str, Any]:
    """Лічильники покриття -> `out` (JSON)."""
    items = result.items.values()
    report: dict[str, Any] = {
        "queried_at": result.queried_at,
        "cache": result.cache_path.name,
        "codes_with_qid": len(result.items),
        "codes_with_population": sum(1 for i in items if i.population is not None),
        "codes_with_coordinates": sum(1 for i in items if i.lat is not None and i.lon is not None),
        "conflicts": len(result.conflicts),
        "oblast_capitals": len(result.capitals),
        "oblast_capital_conflicts": len(result.capital_conflicts),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a != "--refresh" for a in args):
        sys.stderr.write("використання: python -m pipeline.wikidata [--refresh]\n")
        return 2
    try:
        result = fetch(RAW, refresh="--refresh" in args)
    except WikidataError as exc:
        sys.stderr.write(f"wikidata: {exc}\n")
        return 1
    report = build_report(result, REPORT_FILE)
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
