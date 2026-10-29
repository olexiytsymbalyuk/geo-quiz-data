"""Завантаження сирих джерел із замкненими контрольними сумами.

`config/sources.yaml` — що завантажувати (похідний файл має `derived_from` -- походження, яке копіюється в lock); `config/sources.lock` — що саме було завантажено
(url, час, розмір, sha256, для OSM — час даних). Усі наступні кроки конвеєра читають сирі
джерела лише з кешу `data/raw/`, перевіреного цим lock: без `--update` файл береться з кешу
або завантажується, і в обох випадках його sha256 мусить збігтися з lock.

Wikidata сюди не входить: її модуль зберігає дату запиту в імені кешу.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
import yaml

from pipeline.paths import CONFIG, RAW
from pipeline.tools import run_tool

USER_AGENT = "geo-quiz-data/0 (+https://github.com/olexiytsymbalyuk/geo-quiz-data)"
SOURCES = CONFIG / "sources.yaml"
LOCK = CONFIG / "sources.lock"
_CHUNK = 1 << 20
_TIMEOUT = (15, 120)  # (з'єднання, пауза між байтами), секунди
_OSM_TIMESTAMP_QUERIES = ("data.timestamp.last", "header.option.osmosis_replication_timestamp")


class ChecksumMismatch(RuntimeError):
    """sha256 файла не збігається із записаним у `sources.lock`."""


class LockError(RuntimeError):
    """`sources.lock` відсутній, не містить файла або описує інший url, ніж `sources.yaml`."""


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def osm_timestamp(pbf: Path) -> str:
    """Час даних OSM-вивантаження (`osmium fileinfo -e`), ISO 8601 UTC."""
    pbf = pbf.resolve()
    for query in _OSM_TIMESTAMP_QUERIES:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            run_tool(["osmium", "fileinfo", "-e", "-g", query, f"/data/{pbf.name}"], {pbf.parent: "/data"})
        value = out.getvalue().strip()
        if value:
            return value
    raise RuntimeError(f"osmium fileinfo не дав часу даних для {pbf.name}")


def _download(url: str, dest: Path, expected_sha256: str | None = None) -> dict[str, Any]:
    """Потокове завантаження в `dest.part` → `replace`; повертає sha256, size і заголовки.

    З `expected_sha256` сума перевіряється до перейменування: при розбіжності `ChecksumMismatch`,
    `dest` не змінюється (раніше завантажений файл не перезаписується), `.part` видаляється.
    """
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with requests.get(url, stream=True, timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            with part.open("wb") as f:
                for chunk in resp.iter_content(_CHUNK):
                    f.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            info: dict[str, Any] = {"sha256": digest.hexdigest(), "size": size}
            if resp.url != url:
                info["resolved_url"] = resp.url
            if "Last-Modified" in resp.headers:
                info["last_modified"] = resp.headers["Last-Modified"]
        expected = resp.headers.get("Content-Length")
        if expected is not None and resp.headers.get("Content-Encoding") is None and int(expected) != size:
            raise OSError(f"{url}: отримано {size} байт замість {expected}")
        if expected_sha256 is not None and info["sha256"] != expected_sha256:
            raise _mismatch(dest.name, info["sha256"], expected_sha256)
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return info


def _read_lock(lock: Path) -> dict[str, Any]:
    if not lock.exists():
        raise LockError(f"{lock} відсутній; створіть його: python -m pipeline.fetch --update")
    data = yaml.safe_load(lock.read_text(encoding="utf-8")) or {}
    sources: dict[str, Any] = data.get("sources", {})
    return sources


def _mismatch(name: str, actual: str, expected: str) -> ChecksumMismatch:
    return ChecksumMismatch(f"{name}: sha256 {actual} не збігається з lock {expected}")


def fetch_all(sources: Path, lock: Path, raw: Path, update: bool = False) -> dict[str, Path]:
    """Приводить `raw` у відповідність до `sources` і повертає головний файл кожного джерела.

    Без `update`: файл з кешу або завантаження (з `resolved_url` із lock, якщо він є, бо `url` може
    бути рухомим `-latest`); sha256 мусить збігтися з `lock`, інакше `ChecksumMismatch`
    (завантажений файл із хибною сумою не лишається в кеші).
    З `update`: усе завантажується наново, `lock` переписується.
    """
    config: dict[str, Any] = yaml.safe_load(sources.read_text(encoding="utf-8"))["sources"]
    locked = {} if update else _read_lock(lock)
    new_lock: dict[str, Any] = {}
    result: dict[str, Path] = {}

    for source, spec in config.items():
        files: list[dict[str, Any]] = spec["files"]
        entries: dict[str, Any] = {}
        for item in files:
            fname, url = item["name"], item["url"]
            dest = raw / source / fname
            label = f"{source}/{fname}"
            if update:
                entries[fname] = _fetch_fresh(dest, url, spec, item.get("derived_from"))
            else:
                entry = locked.get(source, {}).get("files", {}).get(fname)
                if entry is None:
                    raise LockError(f"{label} немає в {lock}; python -m pipeline.fetch --update")
                if entry["url"] != url:
                    raise LockError(f"{label}: url у {sources.name} ({url}) інший, ніж у lock ({entry['url']})")
                if entry.get("derived_from") != item.get("derived_from"):
                    raise LockError(f"{label}: derived_from у {sources.name} інший, ніж у lock; python -m pipeline.fetch --update")
                # Рухомий url (`-latest`) -- лише для оновлення lock; звичайний fetch тягне знімок.
                _ensure_cached(dest, entry.get("resolved_url", url), entry["sha256"], label)
                entries[fname] = entry
        new_lock[source] = {"license": spec["license"], "files": entries}
        main = spec.get("main", files[0]["name"])
        result[source] = raw / source / main

    if update:
        header = "# Згенеровано: python -m pipeline.fetch --update. Не редагувати вручну.\n"
        body = yaml.safe_dump({"sources": new_lock}, sort_keys=False, allow_unicode=True)
        lock.write_text(header + body, encoding="utf-8")
    return result


def _fetch_fresh(dest: Path, url: str, spec: dict[str, Any], derived_from: dict[str, Any] | None) -> dict[str, Any]:
    fetched_at = _utc_now()
    info = _download(url, dest)
    entry: dict[str, Any] = {"url": url, "fetched_at": fetched_at, **info}
    if derived_from is not None:
        entry["derived_from"] = derived_from  # походження похідного файла (pipeline.land_clip): url і sha256 оригіналу, bbox
    if spec.get("osm_timestamp"):
        entry["osm_timestamp"] = osm_timestamp(dest)
    return entry


def _ensure_cached(dest: Path, url: str, expected: str, label: str) -> None:
    if dest.exists():
        actual = sha256_of(dest)
        if actual != expected:
            raise _mismatch(label, actual, expected)
        return
    _download(url, dest, expected_sha256=expected)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if any(a != "--update" for a in args):
        sys.stderr.write("використання: python -m pipeline.fetch [--update]\n")
        return 2
    paths = fetch_all(SOURCES, LOCK, RAW, update="--update" in args)
    for name, path in paths.items():
        sys.stdout.write(f"{name}: {path} ({os.path.getsize(path)} байт)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
