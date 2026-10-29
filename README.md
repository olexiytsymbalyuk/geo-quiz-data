# geo-quiz-data

Конвеєр даних географічної квіз-платформи для українських школярів: з офіційного кодифікатора
КАТОТТГ, OpenStreetMap і Wikidata збирає атлас «Україна» — тайли PMTiles і газетир (назви,
ієрархія, колишні назви, координати, населення). Тести-ворота (`tests/gates/`) доводять правило
топонімів на готових артефактах; реліз, що не пройшов їх, не публікується.

Репозиторій публічний: газетир є похідною базою даних OpenStreetMap, тож ліцензія ODbL вимагає
відкритості. Назва продукту в коді — лише змінна; технічні ідентифікатори (`geo-quiz-data`,
`pipeline`) від неї не залежать.

## Артефакти

`dist/<версія>/` (версія `YYYY.MM.N`, перша — `2026.11.1`): `ukraine-<версія>.pmtiles`,
`gazetteer-<версія>.sqlite`, `gazetteer-ukraine-<версія>.json`, `manifest.json`, `metrics.json`.
Артефакти й сирі джерела (`data/`, `dist/`) у git не потрапляють.

## Правило топонімів

росія і Білорусь — нейтральна суша без назв. Крим і всі тимчасово окуповані території — Україна,
з офіційними назвами КАТОТТГ. У жодному артефакті немає ключа `name:ru`, чотирьох неукраїнських
літер (U+0401/0451, U+042B/044B, U+042A/044A, U+042D/044D) і російських екзонімів. Єдине місце, де
російські форми записані, — блок-лист у `config/policy.yaml`. Країна ніколи не виводиться з
членства в OSM-relation; назви беруться лише з `name:uk` і КАТОТТГ.

## Розробка

```bash
uv venv -p 3.11 && source .venv/bin/activate
uv pip install -r requirements-dev.txt
python -m pytest -q      # юніт-тести
python -m mypy           # strict
```

### Інструменти (Docker)

`osmium` і `tippecanoe` працюють лише в образі `geo-quiz-tools:1` (`docker/Dockerfile`, Debian
bookworm-slim; нативно на arm64 і на linux/amd64). Кроки конвеєра викликають їх тільки через
`pipeline.tools.run_tool`, яка повертає тривалість і пік пам'яті. Версії в образі:

| Інструмент | Версія | Звідки |
|---|---|---|
| osmium-tool | 1.15.0 (libosmium 2.18.0; apt `1.15.0-1`) | пакет Debian bookworm |
| tippecanoe | 2.79.0 | збірка з `felt/tippecanoe` за тегом `2.79.0`; уміє писати `.pmtiles` |

```bash
python -m pipeline.tools build   # зібрати образ (~40 с без кешу)
```

### Збірка релізу

```bash
python -m pipeline.build --version 2026.11.1 [--force]
```

Одна команда проходить увесь конвеєр з кешу (`data/raw/`, без мережі): fetch за `sources.lock` -> КАТОТТГ ->
витяг OSM -> Wikidata -> газетир -> контур і нейтральна суша -> шари з білим списком -> tippecanoe -> штамп
`dataset_version` -> `metrics.json` -> `manifest.json`. Результат — `dist/<версія>/` з п'ятьма файлами.
Збірка пише в `dist/<версія>.partial/` і наприкінці перейменовує теку; якщо `dist/<версія>/` уже є, потрібен
`--force`; при падінні `.partial` лишається для діагностики. Ворота (`tests/gates/`) окрема команда.

Шари й білий список атрибутів — `pipeline/layers.py` (`LAYER_WHITELIST`), PMTiles і штамп
`dataset_version` — `pipeline/tiles.py`, маніфест — `pipeline/manifest.py`, метрики — `pipeline/metrics.py`.
`metrics.json`: час і пік RAM кроків, покриття газетира за типами й областями, `startable` ярусів 1-4,
розміри файлів, фічі за шарами, `tile_stats`, довжина державного кордону (км), id нейтральних країн.

### Залежності

Версії заморожено `uv pip compile`; сторож `tests/unit/test_requirements_lock.py` стежить, щоб
обидва файли збігалися з `pyproject.toml`:

```bash
uv pip compile pyproject.toml --universal --python-version 3.11 -o requirements.txt
uv pip compile pyproject.toml --extra dev --universal --python-version 3.11 -o requirements-dev.txt
```

## Джерела даних і атрибуція

| Джерело | Що дає | Ліцензія |
|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org/copyright) (Geofabrik, Ukraine PBF) | пункти, адмінполігони, вода, вершини, `name:uk` | ODbL 1.0 — © OpenStreetMap contributors |
| [Natural Earth](https://www.naturalearthdata.com/) 5.1.2 | кордони за POV України, фізична географія, озера, річки | public domain |
| [КАТОТТГ](https://mininfra.gov.ua/diialnist/rozvytok-mistsevoho-samovriaduvannia/kodyfikator-administratyvno-terytorialnykh-odynyts-ta-terytorii-terytorialnykh-hromad) (Мінінфраструктури) | офіційні назви, категорії, ієрархія, коди | CC BY 4.0 (підтверджено на mininfra.gov.ua 29.10.2026) |
| [Wikidata](https://www.wikidata.org/) | P9435 (KATOTTH ID), населення, координати, лейбли й аліаси uk/en | CC0 |
| [GeoNames](https://www.geonames.org/) | точки, населення, feature codes, альтернативні назви | CC BY 4.0 |

GADM і Eurostat GISCO як геометрія не використовуються; geoBoundaries та OCHA COD-AB — лише
для звірки, у випуск не потрапляють.

## Ліцензії

- Газетир (похідна база даних OSM) — ODbL 1.0; решта даних — за таблицею вище.
- Код конвеєра — ліцензію обере власник; поки що всі права застережено.
