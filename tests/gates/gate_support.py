"""Спільна логіка воріт: читання артефактів релізу й чисті перевірки, що повертають опис порушень.

Правило 2.1: у текст порушення ніколи не потрапляє сам рядок (назва, ключ, значення) — лише місце:
`(z, x, y, шар, id)` або файл і позиція. Заборонені літери задаються кодовими точками, без гліфів.
"""

from __future__ import annotations

import gzip
import json
import math
import random
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path
from typing import Any

import mapbox_vector_tile
from pmtiles.reader import MmapSource, Reader, all_tiles
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

from pipeline.fetch import sha256_of
from pipeline.layers import LAYER_WHITELIST
from pipeline.policy import Policy
from pipeline.text import FORBIDDEN_RE, key_without_apostrophes, normalize_key

MANIFEST_NAME = "manifest.json"
NAME_UK = "name:uk"
LANDMASS_LAYER = "landmass"
COUNTRY_LAYER = "country"
MAX_REPORTED = 10

# --- байтовий рівень -------------------------------------------------------------------------------------------

# U+0401/0451, U+042B/044B, U+042A/044A, U+042D/044D.
FORBIDDEN_CODEPOINTS = (0x0401, 0x0451, 0x042B, 0x044B, 0x042A, 0x044A, 0x042D, 0x044D)
FORBIDDEN_UTF8 = tuple(chr(cp).encode("utf-8") for cp in FORBIDDEN_CODEPOINTS)
FORBIDDEN_BYTES_RE = re.compile(b"|".join(re.escape(seq) for seq in FORBIDDEN_UTF8))
# JSON-escape тих самих літер (шість символів `\\u`+4 hex): рядок у файлі не містить їх як UTF-8, але декодується в них.
FORBIDDEN_ESCAPE_RE = re.compile(rb"\\u0(?:401|451|42[abd]|44[abd])", re.IGNORECASE)

# Допустимі ключі `names` у газетирі.
ALLOWED_NAME_KEYS = frozenset({"uk", "uk_aliases", "en", "en_aliases", "local", "former"})
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
_RU_KEY_RE = re.compile(r"^(name[:_])?ru($|[:_\-])")


def forbidden_offsets(data: bytes) -> list[int]:
    """Зсуви UTF-8 послідовностей чотирьох неукраїнських літер і їхніх JSON-escape у сирих байтах."""
    hits = [m.start() for m in FORBIDDEN_BYTES_RE.finditer(data)]
    hits += [m.start() for m in FORBIDDEN_ESCAPE_RE.finditer(data)]
    return sorted(hits)


def safe_id(value: object, fallback: int) -> str:
    """Ідентифікатор фічі для звіту: лише якщо схожий на технічний id, інакше порядковий номер."""
    if isinstance(value, str) and _SAFE_ID_RE.fullmatch(value) and FORBIDDEN_RE.search(value) is None:
        return value
    return f"#{fallback}"


def summarize(found: list[str]) -> str:
    """Перші `MAX_REPORTED` порушень і загальна кількість."""
    shown = "; ".join(found[:MAX_REPORTED])
    more = f" (+{len(found) - MAX_REPORTED})" if len(found) > MAX_REPORTED else ""
    return f"{len(found)} порушень: {shown}{more}"


# --- розбір тайла MVT на рівні protobuf -------------------------------------------------------------------------

_WIRE_VARINT, _WIRE_64BIT, _WIRE_LEN, _WIRE_32BIT = 0, 1, 2, 5
_TILE_LAYERS_FIELD = 3
_LAYER_NAME_FIELD = 1
_LAYER_KEYS_FIELD = 3
_LAYER_VALUES_FIELD = 4
_VALUE_STRING_FIELD = 1


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7


def _fields(data: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    """(номер поля, wire type, значення) для плоского protobuf-повідомлення."""
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire == _WIRE_VARINT:
            value, pos = _varint(data, pos)
            yield field, wire, value
        elif wire == _WIRE_LEN:
            size, pos = _varint(data, pos)
            yield field, wire, data[pos : pos + size]
            pos += size
        elif wire == _WIRE_64BIT:
            pos += 8
        elif wire == _WIRE_32BIT:
            pos += 4
        else:
            raise ValueError(f"невідомий wire type {wire}")


def tile_string_fields(tile: bytes) -> Iterator[tuple[str, str, bytes]]:
    """(шар, роль, байти) для кожного рядкового поля тайла: назва шару, ключі, рядкові значення.

    Геометрія й числові значення (де випадкові байти можуть збігтися зі шаблоном) не віддаються.
    """
    for field, wire, value in _fields(tile):
        if field != _TILE_LAYERS_FIELD or wire != _WIRE_LEN or not isinstance(value, bytes):
            continue
        layer_name = ""
        strings: list[tuple[str, bytes]] = []
        for lfield, lwire, lvalue in _fields(value):
            if lwire != _WIRE_LEN or not isinstance(lvalue, bytes):
                continue
            if lfield == _LAYER_NAME_FIELD:
                layer_name = lvalue.decode("utf-8", errors="replace")
                strings.append(("layer_name", lvalue))
            elif lfield == _LAYER_KEYS_FIELD:
                strings.append(("key", lvalue))
            elif lfield == _LAYER_VALUES_FIELD:
                strings += [("value", v) for f, w, v in _fields(lvalue) if f == _VALUE_STRING_FIELD and isinstance(v, bytes)]
        for role, raw in strings:
            yield layer_name, role, raw


# --- вибірка тайлів -------------------------------------------------------------------------------------------

TileKey = tuple[int, int, int]
FULL_ZOOM_MAX = 8
SAMPLE_RATE = 0.02
SAMPLE_SEED = 20261029

# (мін. довгота, мін. широта, макс. довгота, макс. широта). Крим — з Додатку Б; решта — власний вибір виконавця
# (бриф називає області, але не координати): прямокутники із запасом навколо прикордонної смуги.
BORDER_BBOXES: Mapping[str, tuple[float, float, float, float]] = {
    "Крим": (32.4, 44.3, 36.7, 46.3),
    "Донбас": (36.4, 46.8, 40.3, 50.0),
    "Харківщина": (34.9, 48.6, 38.5, 50.5),
    "Сумщина": (33.0, 50.0, 35.9, 52.4),
    "Чернігівщина": (30.4, 50.3, 33.6, 52.4),
    "Київщина": (29.3, 50.3, 31.4, 51.7),
    "Житомирщина": (27.6, 50.4, 29.8, 51.9),
    "Рівненщина": (25.8, 50.6, 28.2, 51.9),
    "Волинь": (23.6, 50.6, 25.9, 51.9),
}


def tile_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(мін. довгота, мін. широта, макс. довгота, макс. широта) тайла Web Mercator."""
    n = 2.0**z

    def lat(ty: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))

    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def in_border_zone(z: int, x: int, y: int) -> bool:
    west, south, east, north = tile_bounds(z, x, y)
    return any(
        west < b_east and east > b_west and south < b_north and north > b_south
        for b_west, b_south, b_east, b_north in BORDER_BBOXES.values()
    )


def select_tiles(refs: Iterable[TileKey], seed: int = SAMPLE_SEED, rate: float = SAMPLE_RATE) -> set[TileKey]:
    """z <= 8 — усі; z > 8 — усі тайли прикордоння плюс `rate` решти (фіксований seed, відтворювано)."""
    rng = random.Random(seed)
    chosen: set[TileKey] = set()
    for ref in sorted(refs):
        z, x, y = ref
        if z <= FULL_ZOOM_MAX or in_border_zone(z, x, y):
            chosen.add(ref)
        elif rng.random() < rate:
            chosen.add(ref)
    return chosen


# --- артефакти -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TileFeature:
    """Атрибути фічі (без геометрії) і її місце: `index` — порядковий номер у шарі тайла."""

    z: int
    x: int
    y: int
    layer: str
    index: int
    properties: Mapping[str, Any]

    def where(self) -> str:
        return f"({self.z}, {self.x}, {self.y}, {self.layer}, {safe_id(self.properties.get('id'), self.index)})"


def _decode(tile: bytes) -> dict[str, Any]:
    decoded: dict[str, Any] = mapbox_vector_tile.decode(tile, default_options={"y_coord_down": True})
    return decoded


class TileSet:
    """PMTiles-архів релізу: метадані, перелік тайлів, розпаковані тайли, фічі з геометрією й без."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with path.open("rb") as handle:
            self.metadata: dict[str, Any] = Reader(MmapSource(handle)).metadata()
        self._attributes: list[TileFeature] | None = None

    def raw_tiles(self, wanted: set[TileKey] | None = None) -> Iterator[tuple[TileKey, bytes]]:
        """Розпаковані (gzip) тайли архіву; `wanted` обмежує вибірку."""
        with self.path.open("rb") as handle:
            for zxy, data in all_tiles(MmapSource(handle)):
                if wanted is not None and zxy not in wanted:
                    continue
                payload = bytes(data)
                yield zxy, (gzip.decompress(payload) if payload[:2] == b"\x1f\x8b" else payload)

    def refs(self) -> list[TileKey]:
        with self.path.open("rb") as handle:
            return sorted(zxy for zxy, _ in all_tiles(MmapSource(handle)))

    @property
    def attributes(self) -> list[TileFeature]:
        """Атрибути всіх фіч усіх тайлів; декодуються один раз за сесію."""
        if self._attributes is None:
            found: list[TileFeature] = []
            shared: dict[tuple[tuple[str, Any], ...], Mapping[str, Any]] = {}  # та сама фіча повторюється в сотнях тайлів
            for (z, x, y), tile in self.raw_tiles():
                for layer, content in _decode(tile).items():
                    for index, feature in enumerate(content["features"]):
                        props = feature["properties"]
                        found.append(TileFeature(z, x, y, layer, index, shared.setdefault(tuple(props.items()), props)))
            self._attributes = found
        return self._attributes

    def features_with_geometry(self, wanted: set[TileKey]) -> Iterator[tuple[int, int, int, str, int, dict[str, Any]]]:
        """(z, x, y, шар, номер, фіча GeoJSON-подібна) для вибраних тайлів; координати — тайлові."""
        for (z, x, y), tile in self.raw_tiles(wanted):
            for layer, content in _decode(tile).items():
                for index, feature in enumerate(content["features"]):
                    yield z, x, y, layer, index, feature


@dataclass(frozen=True)
class Gazetteer:
    """SQLite-газетир: рядки `entity` як словники (текстові колонки — рядки) і `meta`.

    `repr` прихований: pytest друкує аргументи-фікстури в трасуванні, а в логах не мусить бути ні назв, ні блок-листа.
    """

    path: Path = field(repr=False)
    entities: list[dict[str, Any]] = field(repr=False)
    meta: list[dict[str, Any]] = field(repr=False)


@dataclass(frozen=True)
class PolicyView:
    """Політика для воріт: нормалізований блок-лист і нейтральні країни; `repr` прихований (див. `Gazetteer`)."""

    neutral_countries: frozenset[str] = field(repr=False)
    blocked: frozenset[str] = field(repr=False)
    blocked_sentinel_key: str = field(repr=False)


@dataclass(frozen=True)
class JsonArtifact:
    """Розібраний JSON-артефакт із прихованим `repr`."""

    name: str
    payload: Any = field(repr=False)


def load_gazetteer(path: Path) -> Gazetteer:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        entities = [dict(row) for row in con.execute("select * from entity")]
        meta = [dict(row) for row in con.execute("select * from meta")]
    finally:
        con.close()
    return Gazetteer(path, entities, meta)


def find_one(dataset: Path, pattern: str) -> Path:
    matches = sorted(dataset.glob(pattern))
    if len(matches) != 1:
        raise AssertionError(f"у {dataset.name} очікується рівно один файл {pattern}, знайдено {len(matches)}")
    return matches[0]


# --- ворота: ключі й літери ----------------------------------------------------------------------------------

def json_strings(value: Any, *, keys: bool = True) -> Iterator[str]:
    """Усі рядки JSON-значення; ключі словників теж, якщо `keys`."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if keys:
                yield str(key)
            yield from json_strings(item, keys=keys)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from json_strings(item, keys=keys)


def json_keys(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from json_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from json_keys(item)


def tile_key_violations(features: Iterable[TileFeature]) -> list[str]:
    """Ключ `name:ru*`, голе `name`, будь-який `name:*` крім `name:uk`, невідомий шар чи ключ поза білим списком."""
    found: list[str] = []
    for feature in features:
        allowed = LAYER_WHITELIST.get(feature.layer)
        if allowed is None:
            found.append(f"{feature.where()}: шар поза білим списком")
            continue
        for key in feature.properties:
            if key.startswith("name:ru") or _RU_KEY_RE.match(key):
                found.append(f"{feature.where()}: ключ name:ru")
            elif key == "name" or (key.startswith("name:") and key != NAME_UK):
                found.append(f"{feature.where()}: заборонений ключ name*")
            elif key not in allowed:
                found.append(f"{feature.where()}: ключ поза білим списком шару")
    return found


def gazetteer_key_violations(gazetteer: Gazetteer) -> list[str]:
    found: list[str] = []
    for row in gazetteer.entities:
        names = json.loads(row["names"])
        for key in names:
            if _RU_KEY_RE.match(key) or key.startswith("name"):
                found.append(f"sqlite entity {safe_id(row['id'], 0)}: ключ names.ru")
            elif key not in ALLOWED_NAME_KEYS:
                found.append(f"sqlite entity {safe_id(row['id'], 0)}: ключ names поза переліком")
    return found


def json_key_violations(payload: Any, file_name: str) -> list[str]:
    return [f"{file_name}: ключ name:ru" for key in json_keys(payload) if _RU_KEY_RE.match(key)]


def tile_letter_violations(tileset: TileSet) -> list[str]:
    """Байти рядкових полів розпакованих тайлів і декодовані ключі/значення проти `FORBIDDEN_RE`."""
    found: list[str] = []
    for (z, x, y), tile in tileset.raw_tiles():
        for layer, role, raw in tile_string_fields(tile):
            if forbidden_offsets(raw):
                found.append(f"тайл ({z}, {x}, {y}), шар {safe_id(layer, 0)}: заборонена літера в байтах ({role})")
    for feature in tileset.attributes:
        strings = [*feature.properties, *json_strings(list(feature.properties.values()), keys=False)]
        if any(FORBIDDEN_RE.search(s) for s in strings):
            found.append(f"{feature.where()}: заборонена літера в атрибутах")
    return found


def metadata_letter_violations(metadata: Mapping[str, Any]) -> list[str]:
    blob = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
    return [f"метадані pmtiles: заборонена літера, зсув {offset}" for offset in forbidden_offsets(blob)]


def file_letter_violations(path: Path) -> list[str]:
    """Сирі байти текстового файлу (JSON тощо) і декодовані рядки."""
    data = path.read_bytes()
    found = [f"{path.name}: заборонена літера, зсув {offset}" for offset in forbidden_offsets(data)]
    if path.suffix == ".json":
        payload = json.loads(data)
        if any(FORBIDDEN_RE.search(s) for s in json_strings(payload)):
            found.append(f"{path.name}: заборонена літера в декодованих рядках")
    return found


def sqlite_letter_violations(path: Path) -> list[str]:
    """Усі текстові значення всіх таблиць як сирі UTF-8 байти (`text_factory = bytes`) і DDL схеми."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.text_factory = bytes
    found: list[str] = []
    try:
        for (name,) in con.execute("select name from sqlite_master where type = 'table'").fetchall():
            table = name.decode("utf-8")
            columns = [row[1].decode("utf-8") for row in con.execute(f'pragma table_info("{table}")')]
            for rowid, row in enumerate(con.execute(f'select * from "{table}"'), start=1):
                for column, value in zip(columns, row, strict=True):
                    if isinstance(value, bytes) and forbidden_offsets(value):
                        found.append(f"{path.name}: таблиця {table}, колонка {column}, рядок {rowid}")
        for (sql,) in con.execute("select sql from sqlite_master where sql is not null"):
            if forbidden_offsets(sql):
                found.append(f"{path.name}: схема")
    finally:
        con.close()
    return found


# --- ворота: блок-лист ---------------------------------------------------------------------------------------

def policy_view(policy: Policy) -> PolicyView:
    sentinel_key = normalize_key(policy.blocked_sentinel)
    if sentinel_key is None:
        raise ValueError("сентинел блок-листа містить заборонену літеру")
    return PolicyView(policy.neutral_countries, blocked_keys(policy), sentinel_key)


def blocked_keys(policy: Policy) -> frozenset[str]:
    """Нормалізовані (§5.8) записи блок-листа й сентинел — з `config/policy.yaml` під час виконання."""
    keys: set[str] = set()
    for entry in (*policy.exonym_blocklist, policy.blocked_sentinel):
        key = normalize_key(entry)
        if key is not None:  # запис із забороненою літерою ловить test_no_russian_letters
            keys.update({key, key_without_apostrophes(key)})
    return frozenset(keys)


def is_blocked(name: str, blocked: frozenset[str]) -> bool:
    key = normalize_key(name)
    return key is not None and (key in blocked or key_without_apostrophes(key) in blocked)


def tile_exonym_violations(features: Iterable[TileFeature], blocked: frozenset[str]) -> list[str]:
    cache: dict[str, bool] = {}
    found: list[str] = []
    for feature in features:
        name = feature.properties.get(NAME_UK)
        if not isinstance(name, str):
            continue
        if name not in cache:
            cache[name] = is_blocked(name, blocked)
        if cache[name]:
            found.append(f"{feature.where()}: назва з блок-листа")
    return found


def gazetteer_exonym_violations(gazetteer: Gazetteer, blocked: frozenset[str]) -> list[str]:
    found: list[str] = []
    for row in gazetteer.entities:
        values = [*json_strings(json.loads(row["names"]), keys=False), *json_strings(json.loads(row["facts"]), keys=False)]
        if any(is_blocked(v, blocked) for v in values):
            found.append(f"sqlite entity {safe_id(row['id'], 0)}: назва з блок-листа")
    return found


def json_exonym_violations(payload: Mapping[str, Any], blocked: frozenset[str]) -> list[str]:
    found: list[str] = []
    for index, entity in enumerate(payload.get("entities", [])):
        if any(is_blocked(v, blocked) for v in json_strings(entity, keys=False)):
            found.append(f"json entity {safe_id(entity.get('id'), index)}: назва з блок-листа")
    return found


# --- ворота: нейтральна суша ---------------------------------------------------------------------------------

# Допуск у тайлових одиницях (extent 4096). Контур і нейтральна суша спрощуються окремо, тож уздовж спільного
# кордону між ними лишається шов шириною до ~2 одиниць (виміряно на реліз 2026.11.1: після стиснення суші на 2
# одиниці перетин усіх іменованих ліній і полігонів дорівнює нулю). Лінія чи полігон «усередині», лише якщо
# заходять у глибину суші далі за шов і довжина/площа перетину більша за EPS.
SEAM_TOLERANCE = 2.0
EPS_LENGTH = 1.0
EPS_AREA = 1.0
_POINT_TYPES = frozenset({"Point", "MultiPoint"})
_LINE_TYPES = frozenset({"LineString", "MultiLineString"})


def _valid(geometry: BaseGeometry) -> BaseGeometry:
    return geometry if geometry.is_valid else make_valid(geometry)


def feature_inside_landmass(geometry: dict[str, Any], landmass: BaseGeometry, seam_free: BaseGeometry | None = None) -> bool:
    """Точка — `within`; лінія чи полігон — перетин із сушею без шва довжиною/площею більше за допуск.

    `seam_free` — суша, стиснена на `SEAM_TOLERANCE` (обчислюється, якщо не передана).
    """
    shaped = _valid(shape(geometry))
    kind = geometry["type"]
    if kind in _POINT_TYPES:
        return bool(landmass.contains(shaped))
    deep = landmass.buffer(-SEAM_TOLERANCE) if seam_free is None else seam_free
    overlap = shaped.intersection(deep)
    if kind in _LINE_TYPES:
        return bool(overlap.length > EPS_LENGTH)
    return bool(overlap.area > EPS_AREA)


def neutral_geometry_violations(tileset: TileSet) -> tuple[list[str], int]:
    """(порушення, кількість перевірених тайлів з нейтральною сушею) по вибірці тайлів."""
    wanted = select_tiles(tileset.refs())
    found: list[str] = []
    checked = 0
    # Фічі приходять згрупованими за тайлом: геометрія одного тайла живе лише під час його перевірки.
    for (z, x, y), group in groupby(tileset.features_with_geometry(wanted), key=lambda item: item[:3]):
        items = [(layer, index, feature) for _, _, _, layer, index, feature in group]
        polygons = [shape(f["geometry"]) for layer, _, f in items if layer == LANDMASS_LAYER]
        if not polygons:
            continue
        checked += 1
        landmass = _valid(unary_union([_valid(p) for p in polygons]))
        seam_free = landmass.buffer(-SEAM_TOLERANCE)
        for layer, index, feature in items:
            if layer == LANDMASS_LAYER or NAME_UK not in feature["properties"]:
                continue
            if feature_inside_landmass(feature["geometry"], landmass, seam_free):
                found.append(f"({z}, {x}, {y}, {layer}, {safe_id(feature['properties'].get('id'), index)}): іменована фіча в нейтральній суші")
    return found, checked


def neutral_country_layer_violations(features: Iterable[TileFeature], neutral_ids: frozenset[str]) -> list[str]:
    return [
        f"{f.where()}: нейтральна країна в шарі country"
        for f in features
        if f.layer == COUNTRY_LAYER and f.properties.get("id") in neutral_ids
    ]


def neutral_row_violations(gazetteer: Gazetteer, neutral_countries: frozenset[str]) -> list[str]:
    return [
        f"sqlite entity {safe_id(row['id'], 0)}: країна з нейтрального списку"
        for row in gazetteer.entities
        if row["country"] in neutral_countries
    ]


# --- ворота: маніфест ----------------------------------------------------------------------------------------

def manifest_file_violations(dataset: Path, manifest: Mapping[str, Any]) -> list[str]:
    artifacts: Mapping[str, Mapping[str, Any]] = manifest.get("artifacts", {})
    found: list[str] = []
    for name, entry in sorted(artifacts.items()):
        path = dataset / name
        if not path.is_file():
            found.append(f"{name}: файл з маніфесту відсутній")
            continue
        if path.stat().st_size != entry.get("size"):
            found.append(f"{name}: розмір не збігається")
        if sha256_of(path) != entry.get("sha256"):
            found.append(f"{name}: sha256 не збігається")
    present = {p.name for p in dataset.iterdir()}
    for name in sorted(present - set(artifacts) - {MANIFEST_NAME}):
        found.append(f"{name}: файл поза маніфестом")
    return found


def version_violations(versions: Mapping[str, object], expected: str) -> list[str]:
    """`versions` — джерело -> значення `dataset_version`; усі мають дорівнювати імені теки."""
    return [f"{source}: dataset_version не збігається з {expected}" for source, value in versions.items() if value != expected]
