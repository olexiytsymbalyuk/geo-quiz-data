# geo-quiz-data

Конвеєр даних географічної квіз-платформи для українських школярів: з офіційного кодифікатора
КАТОТТГ, OpenStreetMap і Wikidata збирає атлас «Україна» — тайли PMTiles і газетир (назви,
ієрархія, координати, населення; колишні назви з постанов ВРУ (2016, 2024) — лише у `former[]` газетира, не в тайлах і не в JSON охоплення). Тести-ворота (`tests/gates/`) доводять правило
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

Стара назва з пари перейменувань (`renames_check`) живе лише у `former[]` нової сутності. Однойменні
неперейменовані села, що законно носять таку назву, перелічені в `renames_homonym_exceptions`
(`config/policy.yaml`) за кодами КАТОТТГ — ворота вибачають їх лише за кодом, а не за назвою.

## Розробка

```bash
uv venv -p 3.11 && source .venv/bin/activate
uv pip install -r requirements-dev.txt
python -m pytest -q      # юніт-тести й самотест воріт
python -m pytest tests/gates -q --dataset dist/<версія>   # ворота по артефактах релізу
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

Одна команда проходить увесь конвеєр з кешу (`data/raw/`, без мережі; маска суші — обрізаний asset, див. нижче): fetch за `sources.lock` -> КАТОТТГ ->
витяг OSM -> Wikidata -> газетир -> контур і нейтральна суша -> шари з білим списком -> tippecanoe -> штамп
`dataset_version` -> `metrics.json` -> `manifest.json`. Результат — `dist/<версія>/` з п'ятьма файлами.
Збірка пише в `dist/<версія>.partial/` і наприкінці перейменовує теку; якщо `dist/<версія>/` уже є, потрібен
`--force`; при падінні `.partial` лишається для діагностики. Ворота (`tests/gates/`) окрема команда.

Шари й білий список атрибутів — `pipeline/layers.py` (`LAYER_WHITELIST`), PMTiles і штамп
`dataset_version` — `pipeline/tiles.py`, маніфест — `pipeline/manifest.py`, метрики — `pipeline/metrics.py`.
`metrics.json`: час і пік RAM кроків, покриття газетира за типами й областями, `startable` ярусів 1-4,
розміри файлів, фічі за шарами, `tile_stats`, довжина державного кордону (км), id нейтральних країн.

#### Публікація релізу

Ручний запуск `.github/workflows/build.yml` (вхід — версія `YYYY.MM.N`) збирає датасет, проганяє ворота й лише після
зелених воріт публікує реліз.

- **Кожна версія — власний реліз і тег `dataset-<версія>`** з п'ятьма файлами: `ukraine-<версія>.pmtiles`,
  `gazetteer-<версія>.sqlite`, `gazetteer-ukraine-<версія>.json`, `manifest.json`, `metrics.json`. Реліз створюється один
  раз; workflow не замінює й не видаляє його файлів. Виправлені дані виходять під наступною версією.
- **Повторна збірка тієї самої версії падає** у першому job (`validate`), ще до двогодинної збірки: запуск зупиняється,
  якщо реліз `dataset-<версія>` уже є. Так само він зупиняється, коли GitHub не вдалося запитати (мережа, доступ):
  відсутність релізу має бути підтверджена, а не припущена.
- **Pre-release `m0` — лише для огляду.** Це один плаваючий реліз: кожен успішний запуск оновлює його файлами щойно
  зібраної версії й прибирає файли попередньої. Закріплювати `m0` не слід.
- **Закріплення в застосунку.** Реліз закріплюють трьома значеннями: `version` (версія датасету), `release`
  (`dataset-<версія>`) і `manifest_sha256` (sha256 файла `manifest.json` цього релізу). Усі три workflow виводить у
  підсумку запуску, а sha256 — ще й у нотатках релізу. Застосунок завантажує з релізу `manifest.json`, звіряє його sha256
  із закріпленим, потім завантажує кожен файл з розділу `artifacts` маніфесту й звіряє його sha256 з маніфестом, тож у
  релізі мають бути всі файли, які перелічує маніфест. Перед створенням релізу workflow перевіряє, що в теці артефакту
  лежать рівно ці п'ять файлів.

### Полігони суші (обрізане джерело)

Маска суші — [osmdata land-polygons-split-4326](https://osmdata.openstreetmap.de/data/land-polygons.html) (ODbL 1.0,
© OpenStreetMap contributors). Оригінал (≈ 929 МБ) оновлюється щодня без датованого знімка, тож холодний fetch
завжди падав би на sha256. За рішенням власника від 2026-10-29 конвеєр читає **обрізану похідну** `land-polygons-split-4326-ukraine.zip`
(≈ 5 МБ) — лише полігони, що перетинають bbox датасету + 3° (`CLIP_MARGIN_DEG`), як asset релізу
[`sources-2026-10`](https://github.com/olexiytsymbalyuk/geo-quiz-data/releases/tag/sources-2026-10). Звичайний
`python -m pipeline.fetch` і CI тягнуть саме його; sha256 і розмір — у `config/sources.lock`, походження (url і sha256
оригіналу, bbox, запас) — у `derived_from` там і в `PROVENANCE.json` усередині архіву.

У GitHub Actions (`.github/workflows/build.yml`) кеш `data/raw` має ключ `raw-<хеш sources.lock>`; якщо fetch упав
посередині, недокачане (без `.part`) зберігається під ключем `…-partial-<запуск>`, і наступний запуск продовжує звідти.

Виробити файл наново (потрібен кешований оригінал; кладіть його поза `data/raw/`, щоб ворота знаходили там рівно один архів):

```bash
mkdir -p data/originals && mv <оригінал>.zip data/originals/land-polygons-split-4326.zip
python -m pipeline.land_clip --write-lock   # -> data/release-assets/ (gitignored); виводить розмір і sha256
gh release upload sources-2026-10 data/release-assets/land-polygons-split-4326-ukraine.zip --clobber
```

Вихід відтворюваний байт у байт **в одному середовищі**: записи відсортовано, координати без округлення, дата dbf і метадані
zip зафіксовані, але вміст deflate залежить від версії zlib, а shp/dbf — від pyshp. Актив у релізі зроблено з pyshp 3.1.6,
zlib 1.2.12, Python 3.11.14 (macOS arm64); справжня гарантія — sha256 асета в `sources.lock`, його перевіряє кожен fetch.
Нове виробництво в іншому середовищі може дати інші байти (той самий вміст полігонів) — тоді нова пара sha256 у lock
і новий asset. `load_land_mask` відмовляє на bbox поза обрізкою й перерозпаковує кеш, якщо архів під тим самим іменем змінився.
`python -m pipeline.fetch --update` запис land_polygons не переписує (лише перевіряє) — його пише тільки
`python -m pipeline.land_clip --write-lock`; посилання-редіректи на інші хости (підписані URL GitHub) у lock не потрапляють.
Нове вивантаження osmdata — це нова пара `derived_from` у `config/sources.yaml` (url, sha256, size оригіналу) і новий
тег `sources-YYYY-MM`.

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
| [OSM land polygons](https://osmdata.openstreetmap.de/data/land-polygons.html) (osmdata.openstreetmap.de; обрізана похідна) | берегова лінія: маска суші для контуру | ODbL 1.0 — © OpenStreetMap contributors |
| [Natural Earth](https://www.naturalearthdata.com/) 5.1.2 | кордони за POV України (M0); фізична географія, озера, річки — планується (не в M0) | public domain |
| [КАТОТТГ](https://mininfra.gov.ua/diialnist/rozvytok-mistsevoho-samovriaduvannia/kodyfikator-administratyvno-terytorialnykh-odynyts-ta-terytorii-terytorialnykh-hromad) (Міністерство розвитку громад та територій України (Мінрозвитку)) | офіційні назви, категорії, ієрархія, коди | CC BY 4.0 (підтверджено на mininfra.gov.ua 29.10.2026) |
| [Wikidata](https://www.wikidata.org/) | P9435 (KATOTTH ID), населення, координати, лейбли й аліаси uk/en | CC0 |
| [GeoNames](https://www.geonames.org/) | планується (не в M0): точки, населення, feature codes, альтернативні назви | CC BY 4.0 |

GADM і Eurostat GISCO як геометрія не використовуються; geoBoundaries та OCHA COD-AB — лише
для звірки, у випуск не потрапляють.

## Ліцензії

- Газетир (похідна база даних OSM) і обрізана маска суші (`land-polygons-split-4326-ukraine.zip`, похідна OSM) — ODbL 1.0;
  решта даних — за таблицею вище.
- Код конвеєра — ліцензію обере власник; поки що всі права застережено.
