"""Газетир атласу «Україна»: КАТОТТГ + OSM + Wikidata (бриф §5.3).

Назви — лише з КАТОТТГ (адмінодиниці й населені пункти) або з `name:uk` (вода, вершини).
Країна завжди `UA` (константа, з OSM ніколи не виводиться). Точки й полігони — з OSM за тегом
`katotth`; запасна точка — P625 Wikidata. Крим і всі тимчасово окуповані території — звичайні
одиниці України. Будь-яка назва із забороненою літерою чи з блок-листа екзонімів валить збірку
(у тексті помилки — лише id сутності).

Запуск: `python -m pipeline.gazetteer --report` -> `data/work/gazetteer-report.json`,
`gazetteer.sqlite`, `gazetteer-ukraine.json`.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shapely import contains_xy, prepare
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from pipeline.geo_types import as_multipolygon
from pipeline.katottg import KatottgUnit
from pipeline.letters import chain_letter, compute_startable
from pipeline.policy import Policy
from pipeline.text import has_forbidden, normalize_key
from pipeline.wikidata import WdItem, WikidataResult

COUNTRY = "UA"
SCOPE = "ukraine"
TIERS = (1, 2, 3, 4)
BIG_CITY_POPULATION = 100_000

# Донеччина: центр за КАТОТТГ — Донецьк; у Wikidata два чинні P36 (Ruling 12).
CAPITAL_OVERRIDES: dict[str, str] = {"UA14000000000091971": "UA14080030010048113"}

_TYPE_BY_CATEGORY = {
    "O": "oblast",
    "P": "raion",
    "H": "hromada",
    "K": "city",
    "M": "city",
    "X": "selyshche",
    "C": "selo",
    "B": "city_district",
}
_SETTLEMENTS = frozenset({"K", "M", "X", "C"})
_POLYGON_CATEGORIES = frozenset({"O", "P", "H"})
_PLACE_RANK = {"city": 0, "town": 1, "village": 2, "hamlet": 3}
_OTHER_PLACE_RANK = len(_PLACE_RANK)
_OSM_TYPE_LETTER = {"node": "n", "way": "w", "relation": "r"}
_LAKE_KINDS = frozenset({"lake", "pond", "oxbow"})
_LIMAN_WORD = "лиман"
_NAME_WORD_SPLIT_RE = re.compile("[\\s\u2010-\u2015-]+")
_QID_RE = re.compile(r"^Q\d+$")
_DIGITS_RE = re.compile(r"^\d+$")
_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


class GazetteerError(RuntimeError):
    """Дані порушують правила газетира; у повідомленні лише id сутностей, без назв."""


@dataclass(frozen=True)
class Entity:
    """Сутність схеми §5.3; поля, яких M0 не заповнює, лишаються порожніми."""

    id: str
    type: str
    names: dict[str, Any]
    katottg: str | None = None
    qid: str | None = None
    geonames_id: str | None = None
    osm_id: str | None = None
    parents: list[str] = field(default_factory=list)  # від найближчого предка до області
    country: str = COUNTRY
    lat: float | None = None
    lon: float | None = None
    geom_ref: str | None = None
    population: dict[str, Any] | None = None  # {value, year, source}
    flag: dict[str, Any] | None = None
    coa: dict[str, Any] | None = None
    scopes: list[str] = field(default_factory=lambda: [SCOPE])
    tiers: dict[str, int] = field(default_factory=dict)
    start_letter: str | None = None
    chain_letter: str | None = None
    neighbors: list[str] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    dataset_version: str = ""

    @property
    def tier(self) -> int | None:
        return self.tiers.get(SCOPE)


@dataclass(frozen=True)
class GazetteerResult:
    entities: list[Entity]
    metrics: dict[str, Any]
    startable: dict[int, frozenset[str]]
    dataset_version: str


def resolve_capitals(
    capitals: Mapping[str, str],
    capital_conflicts: Mapping[str, list[str]],
    oblast_codes: Collection[str] | None = None,
) -> dict[str, str]:
    """Центри областей: однозначні з Wikidata + явні перевизначення для конфліктів.

    Якщо передано `oblast_codes`, ключі поза ним (напр. K-міста) ігноруються.
    Конфлікт без перевизначення — `GazetteerError`.
    """

    def relevant(code: str) -> bool:
        return oblast_codes is None or code in oblast_codes

    resolved = {oblast: centre for oblast, centre in capitals.items() if relevant(oblast)}
    for oblast in sorted(capital_conflicts):
        if not relevant(oblast):
            continue
        if oblast not in CAPITAL_OVERRIDES:
            raise GazetteerError(f"кілька центрів для області {oblast} і немає перевизначення")
        resolved[oblast] = CAPITAL_OVERRIDES[oblast]
    return resolved


# --- OSM-кандидати ---


@dataclass(frozen=True)
class _Candidate:
    rank: int
    is_relation_or_way: bool
    osm_numeric_id: int
    ref: str  # n123 / w123 / r123
    feature: Mapping[str, Any]

    @property
    def sort_key(self) -> tuple[int, bool, int]:
        return (self.rank, self.is_relation_or_way, self.osm_numeric_id)


def _props(feature: Mapping[str, Any]) -> Mapping[str, Any]:
    props = feature.get("properties")
    return props if isinstance(props, Mapping) else {}


def _osm_ref(props: Mapping[str, Any]) -> str:
    return f"{_OSM_TYPE_LETTER[props['@type']]}{props['@id']}"


def _candidate(feature: Mapping[str, Any], rank: int) -> _Candidate:
    props = _props(feature)
    return _Candidate(rank, props["@type"] != "node", int(props["@id"]), _osm_ref(props), feature)


def _place_rank(place: object) -> int:
    return _PLACE_RANK.get(str(place), _OTHER_PLACE_RANK)


def _best(candidates: list[_Candidate]) -> _Candidate:
    return min(candidates, key=lambda c: c.sort_key)


def _point(feature: Mapping[str, Any]) -> tuple[float, float] | None:
    """(lat, lon): сама точка або `representative_point()` полігона."""
    geometry = feature.get("geometry")
    if not isinstance(geometry, Mapping):
        return None
    if geometry.get("type") == "Point":
        lon, lat = geometry["coordinates"][:2]
        return float(lat), float(lon)
    polygons = as_multipolygon(shape(geometry))
    if polygons.is_empty:
        return None
    rep = polygons.representative_point()
    return float(rep.y), float(rep.x)


def _osm_population(props: Mapping[str, Any]) -> int | None:
    raw = props.get("population")
    if isinstance(raw, str) and _DIGITS_RE.fullmatch(raw.strip()):
        return int(raw.strip())
    return None


# --- перевірки назв ---


class _NameGuard:
    def __init__(self, policy: Policy) -> None:
        keys = (normalize_key(s) for s in (*policy.exonym_blocklist, policy.blocked_sentinel))
        self._blocked = frozenset(k for k in keys if k is not None)

    def check(self, name: str, entity_id: str) -> None:
        if has_forbidden(name):
            raise GazetteerError(f"заборонена літера в назві сутності {entity_id}")
        if normalize_key(name) in self._blocked:
            raise GazetteerError(f"назва сутності {entity_id} збігається з блок-листом екзонімів")


# --- населені пункти й адмінодиниці ---


def _tier(unit: KatottgUnit, is_centre: bool, population: int | None) -> int | None:
    if unit.category not in _SETTLEMENTS:
        return None
    if unit.category == "K" or is_centre or (population or 0) >= BIG_CITY_POPULATION:
        return 1
    return {"M": 2, "X": 3, "C": 4}[unit.category]


def _base_names(name: str) -> dict[str, Any]:
    return {"uk": name, "en": None, "uk_aliases": [], "en_aliases": [], "local": None, "former": []}


@dataclass
class _OsmIndex:
    places: dict[str, list[_Candidate]] = field(default_factory=dict)  # шар places, лише населені пункти
    admin_places: dict[str, list[_Candidate]] = field(default_factory=dict)  # relation-и з `place`
    polygons: dict[str, list[_Candidate]] = field(default_factory=dict)  # relation-и O/P/H


def _index_osm(
    osm_places: Iterable[Mapping[str, Any]],
    osm_admin: Iterable[Mapping[str, Any]],
    units: Mapping[str, KatottgUnit],
    metrics: Counter[str],
) -> _OsmIndex:
    index = _OsmIndex()
    for layer_is_admin, features in ((False, osm_places), (True, osm_admin)):
        for feature in features:
            props = _props(feature)
            code = props.get("katotth")
            if not isinstance(code, str):
                continue
            unit = units.get(code)
            if unit is None:
                metrics["unknown_katotth"] += 1
            elif layer_is_admin and unit.category in _POLYGON_CATEGORIES:
                index.polygons.setdefault(code, []).append(_candidate(feature, 0))
            elif unit.category in _SETTLEMENTS and "place" in props:
                target = index.admin_places if layer_is_admin else index.places
                target.setdefault(code, []).append(_candidate(feature, _place_rank(props["place"])))
            else:
                metrics["katotth_ignored"] += 1
    return index


def _pick(group: list[_Candidate] | None, metrics: Counter[str]) -> tuple[_Candidate, tuple[float, float]] | None:
    """Найкращий кандидат групи (ранг, node, менший id) і його точка; решта групи -> `duplicate_katotth`.

    Якщо в найкращого немає придатної геометрії, повертає `None` (наступні кандидати не пробуються).
    """
    if not group:
        return None
    metrics["duplicate_katotth"] += len(group) - 1
    best = _best(group)
    point = _point(best.feature)
    return None if point is None else (best, point)


def _population(item: WdItem | None, osm_tag: int | None) -> dict[str, Any] | None:
    if item is not None and item.population is not None:
        return {"value": item.population, "year": item.population_year, "source": "wikidata"}
    if osm_tag is not None:
        return {"value": osm_tag, "year": None, "source": "osm"}
    return None


def _unit_entity(
    unit: KatottgUnit,
    item: WdItem | None,
    index: _OsmIndex,
    centres: frozenset[str],
    version: str,
    metrics: Counter[str],
) -> Entity:
    lat: float | None = None
    lon: float | None = None
    osm_id: str | None = None
    geom_ref: str | None = None
    osm_population: int | None = None
    if unit.category in _SETTLEMENTS:
        picked = _pick(index.places.get(unit.code), metrics) or _pick(index.admin_places.get(unit.code), metrics)
        if picked is not None:
            best, (lat, lon) = picked
            osm_id, osm_population = best.ref, _osm_population(_props(best.feature))
        elif item is not None and item.lat is not None and item.lon is not None:
            lat, lon = item.lat, item.lon
        else:
            metrics["no_point"] += 1
    elif unit.category in _POLYGON_CATEGORIES:
        picked = _pick(index.polygons.get(unit.code), metrics)
        if picked is None:
            metrics["no_polygon"] += 1
        else:
            best, (lat, lon) = picked
            osm_id, geom_ref = best.ref, f"osm:{best.ref}"
    population = _population(item, osm_population)
    return Entity(
        id=f"ua:{unit.code}",
        type=_TYPE_BY_CATEGORY[unit.category],
        names=_base_names(unit.name),
        katottg=unit.code,
        qid=None if item is None else item.qid,
        osm_id=osm_id,
        parents=[f"ua:{code}" for code in reversed(unit.parents)],
        lat=lat,
        lon=lon,
        geom_ref=geom_ref,
        population=population,
        attributes={"is_oblast_centre": unit.code in centres},
        dataset_version=version,
    )


def _assign_tiers(entities: list[Entity], units: Mapping[str, KatottgUnit], metrics: Counter[str]) -> list[Entity]:
    """Яруси й метрики населення для населених пунктів; решта сутностей без змін."""
    result: list[Entity] = []
    for entity in entities:
        unit = units[entity.katottg] if entity.katottg else None
        if unit is None or unit.category not in _SETTLEMENTS:
            result.append(entity)
            continue
        value = None if entity.population is None else int(entity.population["value"])
        tier = _tier(unit, bool(entity.attributes["is_oblast_centre"]), value)
        if tier is None:  # недосяжно: населений пункт завжди має ярус
            raise GazetteerError(f"сутність {entity.id} без ярусу")
        source = "none" if entity.population is None else {"wikidata": "wd", "osm": "osm"}[entity.population["source"]]
        metrics[f"population_{source}"] += 1
        metrics[f"tier_{tier}"] += 1
        result.append(dataclasses.replace(entity, tiers={SCOPE: tier}))
    return result


def _startable_by_tier(entities: list[Entity], min_count: int) -> dict[int, frozenset[str]]:
    return {
        tier: compute_startable(
            (e.names["uk"] for e in entities if e.tier is not None and e.tier <= tier), min_count
        )
        for tier in TIERS
    }


def effective_chain_tier(tier: int, startable: Mapping[int, frozenset[str]]) -> int:
    """Найменший непорожній пул `startable` з ярусом ≥ `tier` (Ruling 22); порожні всі -> `GazetteerError`."""
    for candidate in sorted(t for t in startable if t >= tier):
        if startable[candidate]:
            return candidate
    raise GazetteerError(f"немає непорожнього пулу стартових літер для ярусу {tier}")


def _assign_chain_letters(
    entities: list[Entity], startable: Mapping[int, frozenset[str]], metrics: Counter[str]
) -> list[Entity]:
    result: list[Entity] = []
    for entity in entities:
        tier = entity.tier
        if tier is None:
            result.append(entity)
            continue
        pool_tier = effective_chain_tier(tier, startable)
        if pool_tier != tier:
            metrics["chain_tier_fallback"] += 1
        try:
            letter = chain_letter(entity.names["uk"], startable[pool_tier])
        except ValueError as exc:
            raise GazetteerError(f"у назві сутності {entity.id} немає літер алфавіту") from None
        if letter not in startable[pool_tier]:
            metrics["chain_letter_not_startable"] += 1
        result.append(dataclasses.replace(entity, chain_letter=letter))
    return result


# --- вода й вершини ---


def _water_type(props: Mapping[str, Any], name: str) -> str | None:
    waterway = props.get("waterway")
    if waterway == "river":
        return "river"
    if waterway == "canal":
        return "canal"
    if props.get("natural") != "water":
        return None
    water = props.get("water")
    if water == "lagoon":
        return "liman"
    if water == "reservoir":  # теги мають пріоритет над назвою
        return "reservoir"
    if water is not None and water not in _LAKE_KINDS:
        return None
    if water in (None, "lake") and _LIMAN_WORD in _NAME_WORD_SPLIT_RE.split(name.lower()):
        return "liman"
    return "lake" if water in _LAKE_KINDS else None


def _qid_of(props: Mapping[str, Any]) -> str | None:
    raw = props.get("wikidata")
    return raw if isinstance(raw, str) and _QID_RE.fullmatch(raw) else None


def _ele(props: Mapping[str, Any]) -> int | float | None:
    raw = props.get("ele")
    if not isinstance(raw, str) or not _NUMBER_RE.fullmatch(raw.strip()):
        return None
    number = float(raw)
    return int(number) if number.is_integer() else number


@dataclass(frozen=True)
class _Piece:
    """Один OSM-об'єкт води чи вершини, що має назву."""

    numeric_id: int
    ref: str
    type: str
    name: str
    qid: str | None
    feature: Mapping[str, Any]


def water_entity_id(props: Mapping[str, Any]) -> str:
    """Id сутності води чи вершини: `wd:<QID>` за тегом `wikidata`, інакше `osm:<n|w|r><id>`."""
    qid = _qid_of(props)
    return f"wd:{qid}" if qid else f"osm:{_osm_ref(props)}"


def _group_pieces(pieces: Iterable[_Piece]) -> dict[str, list[_Piece]]:
    """Фічі з однаковим `wikidata` — одна сутність `wd:<QID>`; решта — `osm:<ref>`."""
    groups: dict[str, list[_Piece]] = {}
    for piece in pieces:
        groups.setdefault(water_entity_id(_props(piece.feature)), []).append(piece)
    return groups


def _water_pieces(features: Iterable[Mapping[str, Any]], metrics: Counter[str]) -> Iterator[_Piece]:
    for feature in features:
        props = _props(feature)
        name = props.get("name:uk")
        if not isinstance(name, str) or not name.strip():
            metrics["water_no_name"] += 1
            continue
        kind = _water_type(props, name)
        if kind is None:
            metrics["water_other"] += 1
            continue
        yield _Piece(int(props["@id"]), _osm_ref(props), kind, name, _qid_of(props), feature)


def _peak_pieces(features: Iterable[Mapping[str, Any]], metrics: Counter[str]) -> Iterator[_Piece]:
    for feature in features:
        props = _props(feature)
        name = props.get("name:uk")
        if not isinstance(name, str) or not name.strip():
            metrics["peak_no_name"] += 1
            continue
        yield _Piece(int(props["@id"]), _osm_ref(props), "peak", name, _qid_of(props), feature)


def _piece_entities(
    groups: Mapping[str, list[_Piece]], guard: _NameGuard, version: str, metrics: Counter[str], *, with_geom_ref: bool
) -> list[Entity]:
    entities: list[Entity] = []
    for entity_id in sorted(groups):
        pieces = sorted(groups[entity_id], key=lambda p: (p.numeric_id, p.ref))
        for piece in pieces:
            guard.check(piece.name, entity_id)
        first = pieces[0]
        if len({piece.type for piece in pieces}) > 1:
            metrics["water_mixed_type"] += 1
        point = _point(first.feature) if first.type == "peak" else None
        props = _props(first.feature)
        attributes: dict[str, Any] = {}
        ele = _ele(props) if first.type == "peak" else None
        if ele is not None:
            attributes["ele"] = ele
        entities.append(
            Entity(
                id=entity_id,
                type=first.type,
                names=_base_names(first.name),
                qid=first.qid,
                osm_id=first.ref,
                lat=None if point is None else point[0],
                lon=None if point is None else point[1],
                geom_ref=f"osm:{first.ref}" if with_geom_ref else None,
                attributes=attributes,
                dataset_version=version,
            )
        )
    return entities


# --- збірка ---


def _check_unique(entities: list[Entity]) -> None:
    counts = Counter(e.id for e in entities)
    duplicated = sorted(i for i, n in counts.items() if n > 1)
    if duplicated:
        raise GazetteerError(f"повторювані id сутностей: {', '.join(duplicated[:10])}")


def build_gazetteer(
    units: Iterable[KatottgUnit],
    osm_places: Iterable[Mapping[str, Any]],
    osm_admin: Iterable[Mapping[str, Any]],
    osm_water: Iterable[Mapping[str, Any]],
    osm_peaks: Iterable[Mapping[str, Any]],
    wd: Mapping[str, WdItem],
    capitals: Mapping[str, str],
    policy: Policy,
    version: str,
) -> GazetteerResult:
    """Газетир «Україна». `capitals` — уже розв'язані (`resolve_capitals`); `wd` — без конфліктних кодів."""
    metrics: Counter[str] = Counter()
    unit_list = list(units)
    by_code = {u.code: u for u in unit_list}
    guard = _NameGuard(policy)
    for unit in unit_list:
        guard.check(unit.name, f"ua:{unit.code}")
    index = _index_osm(osm_places, osm_admin, by_code, metrics)
    centres = frozenset(
        centre for oblast, centre in capitals.items() if oblast in by_code and by_code[oblast].category == "O"
    )
    metrics["capital_unknown_code"] = sum(1 for c in centres if c not in by_code)
    base = [_unit_entity(u, wd.get(u.code), index, centres, version, metrics) for u in unit_list]
    entities = _assign_tiers(base, by_code, metrics)
    startable = _startable_by_tier(entities, policy.startable_min)
    entities = _assign_chain_letters(entities, startable, metrics)
    entities += _piece_entities(_group_pieces(_water_pieces(osm_water, metrics)), guard, version, metrics, with_geom_ref=True)
    entities += _piece_entities(_group_pieces(_peak_pieces(osm_peaks, metrics)), guard, version, metrics, with_geom_ref=False)
    _check_unique(entities)
    return GazetteerResult(entities, _final_metrics(entities, metrics), startable, version)


def drop_peaks_outside(result: GazetteerResult, outline: BaseGeometry) -> GazetteerResult:
    """Газетир без вершин поза контуром України (як вода в шарах); кількість відкинутих -- метрика `peak_outside`.

    Вершини з буфера витягу Geofabrik, що лежать за межею контуру (зазвичай у межах кількох км -- генералізація
    кордону), не належать атласові. Вхідний результат не змінюється.
    """
    prepare(outline)
    peaks = [e for e in result.entities if e.type == "peak"]
    outside = {
        e.id
        for e in peaks
        if e.lat is not None and e.lon is not None and not contains_xy(outline, e.lon, e.lat)
    }
    kept = [e for e in result.entities if e.id not in outside]
    metrics = {
        **result.metrics,
        "peak_outside": len(outside),
        "entities": len(kept),
        "entities_by_type": dict(sorted(Counter(e.type for e in kept).items())),
    }
    return dataclasses.replace(result, entities=kept, metrics=metrics)


_COUNTER_KEYS = (
    "no_point",
    "no_polygon",
    "unknown_katotth",
    "duplicate_katotth",
    "katotth_ignored",
    "population_wd",
    "population_osm",
    "population_none",
    "water_no_name",
    "water_other",
    "water_mixed_type",
    "peak_no_name",
    "capital_unknown_code",
    "chain_letter_not_startable",
    "chain_tier_fallback",
)


def _final_metrics(entities: list[Entity], counter: Counter[str]) -> dict[str, Any]:
    metrics: dict[str, Any] = {key: counter[key] for key in _COUNTER_KEYS}
    metrics["entities"] = len(entities)
    metrics["entities_by_type"] = dict(sorted(Counter(e.type for e in entities).items()))
    metrics["tiers"] = {str(tier): counter[f"tier_{tier}"] for tier in TIERS}
    return metrics


# --- запуск на реальних даних ---

DATASET_VERSION = "2026.11.1"


def read_layer(path: Path) -> Iterator[dict[str, Any]]:
    """Потоково читає шар `*.geojsonl` (по фічі на рядок)."""
    with path.open(encoding="utf-8") as fin:
        for line in fin:
            if line.strip():
                feature: dict[str, Any] = json.loads(line)
                yield feature


def build_report(result: GazetteerResult) -> dict[str, Any]:
    """Метрики збірки й `startable` ярусів 1-4 для `gazetteer-report.json`."""
    return {
        "dataset_version": result.dataset_version,
        **result.metrics,
        "startable": {str(tier): sorted(letters) for tier, letters in sorted(result.startable.items())},
        "startable_counts": {str(tier): len(letters) for tier, letters in sorted(result.startable.items())},
    }


def build_from_cache(
    version: str = DATASET_VERSION,
    *,
    units: list[KatottgUnit] | None = None,
    wd: WikidataResult | None = None,
    extract_dir: Path | None = None,
) -> GazetteerResult:
    """Збірка газетира з кешу `data/raw/` і витягу `data/work/extract/` (без мережі).

    `units`, `wd`, `extract_dir` дає оркестратор збірки, щоб не розбирати кодифікатор і кеш Wikidata вдруге.
    """
    from pipeline.katottg import KATOTTG_FILE, parse_katottg
    from pipeline.paths import RAW, WORK
    from pipeline.policy import load_policy
    from pipeline.wikidata import fetch

    units = parse_katottg(KATOTTG_FILE) if units is None else units
    wd = fetch(RAW) if wd is None else wd
    capitals = resolve_capitals(wd.capitals, wd.capital_conflicts, {u.code for u in units if u.category == "O"})
    extract = WORK / "extract" if extract_dir is None else extract_dir
    return build_gazetteer(
        units,
        read_layer(extract / "places.geojsonl"),
        read_layer(extract / "admin.geojsonl"),
        read_layer(extract / "water.geojsonl"),
        read_layer(extract / "peaks.geojsonl"),
        wd.items,
        capitals,
        load_policy(),
        version,
    )


def main(argv: list[str] | None = None) -> int:
    """`python -m pipeline.gazetteer --report` — збірка з кешу (без мережі) у `data/work/`."""
    from pipeline.gazetteer_io import write_scope_json, write_sqlite
    from pipeline.paths import WORK

    args = sys.argv[1:] if argv is None else argv
    if args != ["--report"]:
        sys.stderr.write("використання: python -m pipeline.gazetteer --report\n")
        return 2
    result = build_from_cache()
    write_sqlite(result, WORK / "gazetteer.sqlite")
    write_scope_json(result, WORK / "gazetteer-ukraine.json")
    report = build_report(result)
    (WORK / "gazetteer-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
