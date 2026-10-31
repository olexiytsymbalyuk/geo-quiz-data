"""Крихітний світ для самотесту воріт задачі 14: газетир, еталони `data/raw` (КАТОТТГ, NE, маска суші).

Газетир пишеться справжнім `write_sqlite`, тож схема збігається з реальною. Лічильники збігаються з Додатком Б
(27 / 136 / 1469 / 463), літери рахуються справжніми `compute_startable` і `chain_letter`: «добрий» світ проходить
усі ворота, а кожне порушення змінює рівно одну річ.
"""

from __future__ import annotations

import dataclasses
import io
import zipfile
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import openpyxl
import shapefile
import yaml
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient

from pipeline.gazetteer import SCOPE, TIERS, Entity, GazetteerResult, effective_chain_tier
from pipeline.gazetteer_io import write_sqlite
from pipeline.letters import ALPHABET, chain_letter, compute_startable
from pipeline.paths import CONFIG
from pipeline.policy import load_policy
from tests.gates.geo_support import CRIMEA_ANCHORS, FINEST_ZOOM, tile_to_lonlat, tiles_covering

EXTENT = 4096
ARK_NAME = "Автономна Республіка Крим"
KYIV = ("Київ", 80, 93317)
SEVASTOPOL = ("Севастополь", 85, 65278)
VINNYTSIA = ("Вінниця", 5, 10357)
CRIMEA_CITY_NAMES = ("Сімферополь", "Джанкой", "Євпаторія", "Керч", "Феодосія", "Ялта")
OBLASTS, RAIONS, HROMADAS, CRIMEAN_HROMADAS, CITIES = 25, 136, 1469, 3, 463
FILLER_FIRST_LETTERS = "бвгдклмнпрст"
OBLAST_CENTRES = frozenset({"Сімферополь", "Вінниця"})
_CONSONANTS, _VOWELS = "бвгдклмнпрстх", "аеіоу"
_CATEGORY = {"oblast": "O", "raion": "P", "hromada": "H", "city": "M", "selyshche": "X", "selo": "C"}

KATOTTG_FILE = ("katottg", "kodifikator-07-07.xlsx")
NE_FILE = ("ne_admin0_ukr", "ne_10m_admin_0_countries_ukr.shp")
MASK_FILE = ("land_polygons", "land-polygons-split-4326.zip")

# Зона перевірки Криму й межа з сусідом — тайли z12; прямокутники в тайлових одиницях (extent 4096).
CRIMEA_TILE = (12, 2434, 1473)
BORDER_TILE = (12, 2321, 1389)
TAMAN_POINT = (36.66, 45.2)  # «чужа» суша в bbox Криму: компонента маски, що перетинає NE-Росію
BORDER_EDGE_X = 2048
NE_EDGE_SHIFT = 1900  # зсув межі в еталоні NE у «поганому» світі: ≈ 2,9 км на широті 50°
TAIL_SPAN = (1500, 1900)  # ділянка межі (вертикаль тайла), яку «хвіст» виносить убік
TAIL_OFFSET = 3300  # ≈ 5 км
NEUTRAL_BOX = (27.0, 53.0, 30.0, 55.0)  # «Білорусь» в еталоні NE: далеко від решти тайлів, щоб не впливати на інші ворота
NEUTRAL_INTERIOR_POINT = (54.0, 28.5)  # (широта, довгота) -- десятки кілометрів углиб нейтральної суші
VINNYTSIA_POINT = (49.23, 28.47)  # (широта, довгота) у «доброму» світі: поза нейтральною сушею
SPIKE_AT = (6900, 2000)  # острівець за межею тайла праворуч: ≈ 7,5 км від межі


def point_tile(lon: float, lat: float) -> tuple[int, int, int]:
    (tile,) = tiles_covering(FINEST_ZOOM, (lon, lat, lon, lat))
    return tile


ANCHOR_TILES = tuple(point_tile(lon, lat) for lon, lat in CRIMEA_ANCHORS)  # Тузла, Перекоп, Арабатська стрілка
TUZLA_TILE = ANCHOR_TILES[0]
TAMAN_TILE = point_tile(*TAMAN_POINT)


def _code(prefix: int, serial: int) -> str:
    return f"UA{prefix:02d}{serial:015d}"


def _title(index: int, first: str | None = None) -> str:
    syllables = [_CONSONANTS[(index // 65**n) % 65 // 5] + _VOWELS[(index // 65**n) % 5] for n in range(3)]
    word = "".join(syllables)
    return (first or word[0]).upper() + word[1:]


def _names(uk: str, former: Sequence[dict[str, str]] = ()) -> dict[str, object]:
    return {"uk": uk, "en": None, "uk_aliases": [], "en_aliases": [], "local": None, "former": list(former)}


def _entity(
    code: str, kind: str, name: str, parents: Sequence[str], version: str, tier: int | None = None,
    former: Sequence[dict[str, str]] = (),
) -> Entity:
    return Entity(
        id=f"ua:{code}",
        type=kind,
        names=_names(name, former),
        katottg=code,
        parents=[f"ua:{p}" for p in parents],
        tiers={} if tier is None else {SCOPE: tier},
        attributes={"is_oblast_centre": name in OBLAST_CENTRES},
        dataset_version=version,
    )


def _oblasts(version: str) -> list[Entity]:
    oblasts = [_entity(_code(1, 1), "oblast", ARK_NAME, [], version)]
    oblasts += [_entity(_code(k, k), "oblast", f"Область {_title(1000 + k)}", [], version) for k in range(2, OBLASTS + 1)]
    return oblasts


def _raions(oblasts: Sequence[Entity], version: str) -> list[Entity]:
    """Райони 0 і 1 — кримські; решта розкладені по інших областях."""
    result: list[Entity] = []
    for j in range(RAIONS):
        oblast = oblasts[0] if j < 2 else oblasts[1 + (j - 2) % (OBLASTS - 1)]
        prefix = _prefix(oblast)
        result.append(_entity(_code(prefix, 1000 + j), "raion", f"Район {_title(2000 + j)}", [_kt(oblast)], version))
    return result


def _kt(entity: Entity) -> str:
    assert entity.katottg is not None
    return entity.katottg


def _prefix(entity: Entity) -> int:
    return int(_kt(entity)[2:4])


def _parent_codes(parent: Entity) -> list[str]:
    return [_kt(parent), *(p.removeprefix("ua:") for p in parent.parents)]


def _hromadas(raions: Sequence[Entity], version: str) -> list[Entity]:
    crimean = [
        _entity(_code(1, 10000 + j), "hromada", f"Громада {_title(3000 + j)}", _parent_codes(raions[j % 2]), version)
        for j in range(CRIMEAN_HROMADAS)
    ]
    rest = []
    for j in range(HROMADAS):
        raion = raions[2 + j % (RAIONS - 2)]
        prefix = _prefix(raion)
        rest.append(_entity(_code(prefix, 20000 + j), "hromada", f"Громада {_title(4000 + j)}", _parent_codes(raion), version))
    return crimean + rest


def _city(code: str, name: str, parent: Entity, version: str, tier: int) -> Entity:
    return _entity(code, "city", name, _parent_codes(parent), version, tier)


def _cities(hromadas: Sequence[Entity], version: str) -> list[Entity]:
    k_cities = [
        _entity(_code(prefix, serial), "city", name, [], version, 1) for name, prefix, serial in (KYIV, SEVASTOPOL)
    ]
    crimean = [
        _city(_code(1, 30000 + j), name, hromadas[j % CRIMEAN_HROMADAS], version, 1 if name in OBLAST_CENTRES else 2)
        for j, name in enumerate(CRIMEA_CITY_NAMES)
    ]
    vinnytsia_parent = next(h for h in hromadas if _prefix(h) == 5)
    named = [_city(_code(VINNYTSIA[1], VINNYTSIA[2]), VINNYTSIA[0], vinnytsia_parent, version, 1)]
    fixed = len(k_cities) + len(crimean) + len(named)
    ordinary = [h for h in hromadas[CRIMEAN_HROMADAS:]]
    filler = []
    for i in range(CITIES - fixed):
        parent = ordinary[i % len(ordinary)]
        name = _title(5000 + i, FILLER_FIRST_LETTERS[i % len(FILLER_FIRST_LETTERS)])
        filler.append(_city(_code(_prefix(parent), 40000 + i), name, parent, version, 2))
    return k_cities + crimean + named + filler


def _renamed_villages(hromadas: Sequence[Entity], version: str) -> list[Entity]:
    """Сутність із новою назвою для кожної пари `policy.renames_check`: пункт ярусу 4 типу `selyshche` із `former[]`.

    Тип `selyshche` (не `selo`), щоб однойменні села-винятки відрізнялися від перейменованої сутності за типом.
    Пара з `katottg` отримує саме цей код: ворота шукають перейменовану сутність за ним.
    """
    parent = hromadas[CRIMEAN_HROMADAS]
    return [
        _entity(
            rename.katottg or _code(_prefix(parent), 50000 + i), "selyshche", rename.new, _parent_codes(parent), version, 4,
            [{"name": rename.old, "kind": rename.kind, "until": rename.date}],
        )
        for i, rename in enumerate(load_policy().renames_check)
    ]


def _exception_old_names() -> dict[str, str]:
    """Код КАТОТТГ -> назва села-винятку (`old` його пари): `Policy` віддає лише коди, назви беруться з YAML."""
    raw = yaml.safe_load((CONFIG / "policy.yaml").read_text(encoding="utf-8"))
    names = {item["katottg"]: item["name"] for item in raw["renames_homonym_exceptions"]}
    return {code: names[code] for code in sorted(load_policy().renames_homonym_exceptions)}


def _exception_villages(hromadas: Sequence[Entity], version: str) -> list[Entity]:
    """Справжні села-винятки R42: `selo` зі справжнім кодом КАТОТТГ і старою формою як канонічною назвою."""
    parent = hromadas[CRIMEAN_HROMADAS]
    return [
        _entity(code, "selo", name, _parent_codes(parent), version, 4) for code, name in _exception_old_names().items()
    ]


def _with_letters(entities: list[Entity], min_count: int) -> tuple[list[Entity], dict[int, frozenset[str]]]:
    startable = {
        tier: compute_startable((e.names["uk"] for e in entities if e.tier is not None and e.tier <= tier), min_count)
        for tier in TIERS
    }
    lettered = []
    for e in entities:
        if e.tier is None:
            lettered.append(e)
            continue
        pool = startable[effective_chain_tier(e.tier, startable)]
        lettered.append(dataclasses.replace(e, chain_letter=chain_letter(e.names["uk"], pool)))
    return lettered, startable


def good_world(version: str) -> tuple[list[Entity], dict[int, frozenset[str]]]:
    """Сутності й `startable` «доброго» світу."""
    oblasts = _oblasts(version)
    raions = _raions(oblasts, version)
    hromadas = _hromadas(raions, version)
    entities = [
        *oblasts, *raions, *hromadas, *_cities(hromadas, version), *_renamed_villages(hromadas, version),
        *_exception_villages(hromadas, version),
    ]
    lat, lon = VINNYTSIA_POINT
    entities = _replace(entities, _id_of(entities, VINNYTSIA[0]), lat=lat, lon=lon)
    return _with_letters(entities, load_policy().startable_min)


def _replace(entities: Sequence[Entity], entity_id: str, **changes: object) -> list[Entity]:
    return [dataclasses.replace(e, **changes) if e.id == entity_id else e for e in entities]  # type: ignore[arg-type]


def _id_of(entities: Sequence[Entity], name: str) -> str:
    return next(e.id for e in entities if e.names["uk"] == name)


def apply_violation(
    violation: str | None, entities: list[Entity], startable: dict[int, frozenset[str]]
) -> tuple[list[Entity], dict[int, frozenset[str]]]:
    """Порушення газетира; решта порушень (тайли, еталони) додаються в `bad_dataset`."""
    if violation == "crimea_country_ru":
        return _replace(entities, _id_of(entities, "Сімферополь"), country="RU"), startable
    if violation == "gazetteer_point_in_neutral":  # координати Вінниці -- глибоко в нейтральній суші еталона NE
        lat, lon = NEUTRAL_INTERIOR_POINT
        return _replace(entities, _id_of(entities, VINNYTSIA[0]), lat=lat, lon=lon), startable
    if violation == "districts_120":
        dropped = {e.id for e in entities if e.type == "raion"}
        keep = sorted(dropped)[: RAIONS - 16]
        return [e for e in entities if e.id not in dropped or e.id in keep], startable
    if violation == "startable_soft_sign":
        return entities, {**startable, 2: startable[2] | {"ь"}}
    if violation == "chain_letter_outside_pool":
        outside = next(letter for letter in ALPHABET if all(letter not in pool for pool in startable.values()))
        target = next(e.id for e in entities if e.tier == 2)
        return _replace(entities, target, chain_letter=outside), startable
    if violation == "katottg_name_differs":  # назва в газетирі відрізняється від еталона КАТОТТГ
        target = next(e for e in entities if e.type == "hromada")
        return _replace(entities, target.id, names=_names("Невірна назва")), startable
    return _apply_rename_violation(violation, entities), startable


def _apply_rename_violation(violation: str | None, entities: list[Entity]) -> list[Entity]:
    """Порушення воріт перейменувань; еталон КАТОТТГ будується з «доброго» світу, тож код поза ним дав би другий збій."""
    renames = load_policy().renames_check
    if violation == "former_missing":
        return _replace(entities, _id_of(entities, renames[0].new), names=_names(renames[0].new))
    if violation == "homonym_without_exception":  # однойменне село без коду: винятку за кодом йому не дістатися
        stray = Entity(id="ua:UA05000000000099999", type="selo", names=_names(renames[0].old), dataset_version=entities[0].dataset_version)
        return [*entities, stray]
    if violation == "exception_code_unknown":
        dropped = f"ua:{sorted(load_policy().renames_homonym_exceptions)[0]}"
        return [e for e in entities if e.id != dropped]
    if violation == "rename_code_unresolved":  # перейменована сутність без коду: код пари ні до чого не веде
        coded = next(rename for rename in renames if rename.katottg is not None)
        return _replace(entities, _id_of(entities, coded.new), katottg=None)
    return entities


def write_gazetteer(path: Path, entities: list[Entity], startable: dict[int, frozenset[str]], version: str) -> None:
    write_sqlite(GazetteerResult(entities, {}, startable, version), path)


# --- еталони data/raw -------------------------------------------------------------------------------------------

def tile_rectangle(tile: tuple[int, int, int], u0: float, v0: float, u1: float, v1: float) -> Polygon:
    """Прямокутник у тайлових одиницях як полігон у градусах."""
    z, x, y = tile
    ring = np.array([[u0, v0], [u1, v0], [u1, v1], [u0, v1]], dtype=np.float64)
    return Polygon(tile_to_lonlat(z, x, y, EXTENT)(ring))


def write_katottg(raw: Path, entities: Sequence[Entity]) -> None:
    titles = ["Перший рівень", "Другий рівень", "Третій рівень", "Четвертий рівень", "Додатковий рівень"]
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(["Кодифікатор адміністративно-територіальних одиниць (випробувальний)"])
    sheet.append([*titles, "Категорія об'єкта", "Назва об'єкта"])
    for e in entities:
        if e.katottg is None:
            continue
        chain = [p.removeprefix("ua:") for p in reversed(e.parents)] + [e.katottg]
        category = "K" if e.katottg.startswith(("UA80", "UA85")) else _CATEGORY[e.type]
        sheet.append([*chain, *[None] * (len(titles) - len(chain)), category, e.names["uk"]])
    target = raw.joinpath(*KATOTTG_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    book.save(target)


def tile_polygon(tile: tuple[int, int, int], points: Sequence[tuple[int, int]]) -> Polygon:
    """Многокутник за вершинами в тайлових одиницях як полігон у градусах."""
    z, x, y = tile
    return Polygon(tile_to_lonlat(z, x, y, EXTENT)(np.array(points, dtype=np.float64)))


def _ne_border_polygons(path: Sequence[tuple[int, int]]) -> list[tuple[str, Polygon]]:
    """Україна праворуч від ламаної `path` (зверху вниз), сусід `PL` ліворуч; права межа України — край тайла."""
    top, bottom = path[0][1], path[-1][1]
    right = max(EXTENT, *(u + 1 for u, _ in path))
    return [
        ("UA", tile_polygon(BORDER_TILE, [*path, (right, bottom), (right, top)])),
        ("PL", tile_polygon(BORDER_TILE, [*path, (-EXTENT, bottom), (-EXTENT, top)])),
    ]


def ne_polygons(deviation: str | None) -> list[tuple[str, Polygon]]:
    """NE-еталон: Україна = Крим + права частина тайла межі; сусід = ліва. `deviation` псує еталон так,
    щоб порушити рівно один із порогів відхилення (затверджені пороги):

    * `shift` -- вся межа зсунута на ≈ 2,9 км: лише p50;
    * `tail` -- 0,6 км межі винесено на ≈ 5 км (виступ): «хвіст» -- лише p95;
    * `spike` -- окремий острівець України за 7,5 км від межі, що торкається другого сусіда: один викид -- лише max.
    """
    edge = BORDER_EDGE_X + (NE_EDGE_SHIFT if deviation == "shift" else 0)
    path = [(edge, 0), (edge, EXTENT)]
    if deviation == "tail":
        near, far = TAIL_SPAN
        path = [(edge, 0), (edge, near), (edge + TAIL_OFFSET, near), (edge + TAIL_OFFSET, far), (edge, far), (edge, EXTENT)]
    extras: list[tuple[str, Polygon]] = []
    if deviation == "spike":
        u, v = SPIKE_AT
        extras = [
            ("UA", tile_polygon(BORDER_TILE, [(u, v), (u + 20, v), (u + 20, v + 20), (u, v + 20)])),
            ("SK", tile_polygon(BORDER_TILE, [(u + 20, v), (u + 80, v), (u + 80, v + 20), (u + 20, v + 20)])),
        ]
    return [
        ("UA", tile_rectangle(CRIMEA_TILE, 0, 0, EXTENT, EXTENT)),
        *_ne_border_polygons(path),
        ("RU", tile_rectangle(TAMAN_TILE, 0, 0, EXTENT, EXTENT)),
        ("BY", box(*NEUTRAL_BOX)),
        *extras,
    ]


def write_natural_earth(raw: Path, deviation: str | None = None) -> None:
    target = raw.joinpath(*NE_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    with shapefile.Writer(str(target)) as writer:
        writer.field("ISO_A2_EH", "C", size=2)
        for iso, polygon in ne_polygons(deviation):
            writer.poly([list(orient(polygon, -1.0).exterior.coords)])  # зовнішнє кільце за годинниковою стрілкою
            writer.record(iso)


def land_mask_polygons() -> list[Polygon]:
    """Маска суші: тайл Криму, тайли-якорі (острів Тузла поза NE-Україною), тайл «Тамані» (без `land`) і тайл межі."""
    tiles = [CRIMEA_TILE, BORDER_TILE, TAMAN_TILE, *ANCHOR_TILES]
    return [box(*tile_rectangle(tile, 0, 0, EXTENT, EXTENT).bounds) for tile in tiles]


def write_land_mask(raw: Path) -> None:
    parts = {suffix: io.BytesIO() for suffix in ("shp", "shx", "dbf")}
    with shapefile.Writer(shp=parts["shp"], shx=parts["shx"], dbf=parts["dbf"]) as writer:
        writer.field("id", "N")
        for index, polygon in enumerate(land_mask_polygons()):
            writer.poly([list(orient(polygon, -1.0).exterior.coords)])
            writer.record(index)
    target = raw.joinpath(*MASK_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w") as archive:
        for suffix, buffer in parts.items():
            archive.writestr(f"land_polygons.{suffix}", buffer.getvalue())


def write_raw(raw: Path, entities: Sequence[Entity], *, ne_deviation: str | None = None) -> None:
    write_katottg(raw, entities)
    write_natural_earth(raw, ne_deviation)
    write_land_mask(raw)
