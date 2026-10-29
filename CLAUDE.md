# geo-quiz-data — інструкції для Claude Code

Конвеєр даних географічної квіз-платформи для учнів 5–11 класів: КАТОТТГ ⨝ OSM ⨝ Wikidata → PMTiles
і газетир. Коментарі, коміти, спеки й плани — **українською**; ідентифікатори — англійською.
Founding brief — `docs/founding-brief.md` застосунку (`/Users/olexiytsymbalyuk/WebstormProjects/geo-quiz`);
еталонний код — `/Users/olexiytsymbalyuk/WebstormProjects/bot-arena` (лише читати).

## Назва продукту — змінна
Назва продукту (поточна — «Гео-Арена») **робоча**, остаточну обирає власник. У цьому репозиторії
вона ніде не хардкодиться: технічні ідентифікатори (`geo-quiz-data`, `pipeline`, імена артефактів)
від неї не залежать. У документах пиши «платформа».

## Стек і команди
Python 3.11, uv, pytest, **mypy strict**, openpyxl, duckdb, shapely 2, pyproj, pyshp, requests, pyyaml,
pmtiles, mapbox-vector-tile; важкі інструменти (osmium, tippecanoe) — у власному Docker-образі.
Тут немає бекенду й фронтенду — лише конвеєр, тести-ворота й артефакти.

```bash
source .venv/bin/activate
python -m pytest -q      # tests/unit + tests/gates_selftest (швидкі, на синтетичних даних)
python -m pytest tests/gates -q --dataset dist/<версія>   # ворота по готовому релізу (без --dataset — UsageError)
python -m mypy           # strict, пакет pipeline
```
Залежності пінені `uv pip compile` у `requirements.txt` / `requirements-dev.txt`; сторож —
`tests/unit/test_requirements_lock.py`. ESLint, Prettier, ruff, black не використовуємо.

## Правила, які не обговорюються
1. **Топоніми й кордони.** росія і Білорусь — нейтральна суша без назв, без інтерактиву, без відповідей, у всіх атласах.
   Крим і всі тимчасово окуповані території — Україна, офіційні назви КАТОТТГ (з перейменуваннями 2016 і 2024).
   Кордон України малюється повністю. Жодного `name:ru` і жодної з чотирьох неукраїнських літер
   (кодові точки `U+0401/0451`, `U+042B/044B`, `U+042A/044A`, `U+042D/044D`; тут навмисно без гліфів) на рівні байтів —
   у тайлах, газетирі, фікстурах, тестах, коментарях, документах. У коді й тестах ці літери — лише `\u`-escape.
   Російські екзоніми (кирилицею й латинською транслітерацією) існують лише в `config/policy.yaml: exonym_blocklist`;
   сторож `tests/unit/test_repo_text.py` виключає рівно цей файл. Країна ніколи не виводиться з OSM-relation.
   Назва — лише `name:uk`/КАТОТТГ; голе `name` і `name:*`, крім `name:uk`, не читаються ніде.
   Чужі basemap (зокрема готові Protomaps) заборонені. Усе це — автотести, не ручний перегляд.
2. **Тест-ворота.** Реліз публікується, лише коли `tests/gates/` зелені на артефактах `dist/<версія>/`:
   `test_no_neutral_country`, `test_no_name_ru_key`, `test_no_russian_letters`, `test_crimea_is_ukraine`,
   `test_exonym_blocklist`, `test_occupied_uses_katottg`, `test_counts`, `test_borders_full`,
   `test_chain_letters`, `test_manifest` (повний перелік — бриф, Додаток Б). Атрибути тайлів — лише з білого списку.
3. **Джерела й ліцензії.** OSM (ODbL), Natural Earth (public domain), КАТОТТГ (CC BY 4.0), Wikidata (CC0),
   GeoNames (CC BY 4.0) — з атрибуцією (README, «Про проєкт» у застосунку). GADM і GISCO як геометрія — ніколи;
   GISCO, OCHA COD-AB — лише валідація. Сирі джерела й артефакти (`data/`, `dist/`) ніколи не комітяться.
4. **Версія датасету** `YYYY.MM.N` (`pipeline.paths.validate_version`); версії джерел фіксує `config/sources.lock`.
5. **Захист дітей.** Дані учнів сюди не потрапляють; без мілітарних слів у текстах для дітей.

## Процес
- **TDD:** тест пишеться й падає до реалізації; кожен крок — чиста функція-ядро з юніт-тестом на синтетичних
  даних і тонка обгортка над реальними файлами. mypy strict — частина «зелено».
- Дизайн → спека, план → завдання (`### Завдання N:`) у репозиторії `geo-quiz`; виконання — subagent-driven-development.
- Коміт після кожного завдання, `feat(data): …` українською, з трейлером `Co-Authored-By`.
  **Жодних push**, поки власник не дозволить; один push на завершений зелений пакет.
- Великі завантаження (сотні MB) кешуються в `data/raw/` (gitignored) і не перезавантажуються без потреби.

## Де що лежить
`pipeline/` — модулі конвеєра (`policy.py` — політика, `paths.py` — шляхи й версія); `config/` — `policy.yaml`,
`sources.lock`; `tests/unit/` — юніт-тести й сторожі; `tests/gates/` — ворота на артефактах; `data/` і `dist/` — gitignored.
