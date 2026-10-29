"""Тести завантаження джерел із замкненими контрольними сумами (локальний http.server, без мережі)."""

from __future__ import annotations

import functools
import hashlib
import http.server
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from pipeline import fetch
from pipeline.fetch import ChecksumMismatch, LockError, fetch_all

PAYLOAD_A = b"A" * 70_000 + b"kodyfikator"
PAYLOAD_B = b"shape-bytes"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@dataclass
class Site:
    root: Path
    base: str
    server: http.server.ThreadingHTTPServer

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    root = tmp_path / "www"
    root.mkdir()
    (root / "a.xlsx").write_bytes(PAYLOAD_A)
    (root / "b.shp").write_bytes(PAYLOAD_B)
    (root / "b.dbf").write_bytes(b"dbf")
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    s = Site(root, f"http://127.0.0.1:{server.server_address[1]}", server)
    yield s
    try:
        s.stop()
    except Exception:  # сервер уже зупинено тестом
        pass


def _write_sources(path: Path, base: str) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "sources": {
                    "katottg": {
                        "license": "CC BY 4.0",
                        "files": [{"name": "a.xlsx", "url": f"{base}/a.xlsx"}],
                    },
                    "shapes": {
                        "license": "public domain",
                        "main": "b.shp",
                        "files": [
                            {"name": "b.shp", "url": f"{base}/b.shp"},
                            {"name": "b.dbf", "url": f"{base}/b.dbf"},
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_update_writes_lock_with_sha256(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"

    paths = fetch_all(sources, lock, raw, update=True)

    assert paths["katottg"] == raw / "katottg" / "a.xlsx"
    assert paths["shapes"] == raw / "shapes" / "b.shp"  # `main` вказує файл, який повертається
    assert paths["katottg"].read_bytes() == PAYLOAD_A
    entry = yaml.safe_load(lock.read_text(encoding="utf-8"))["sources"]["katottg"]["files"]["a.xlsx"]
    assert entry["sha256"] == hashlib.sha256(PAYLOAD_A).hexdigest()
    assert entry["size"] == len(PAYLOAD_A)
    assert entry["url"] == f"{site.base}/a.xlsx"
    assert entry["fetched_at"].endswith("Z")
    assert "last_modified" in entry  # http.server віддає Last-Modified
    assert not list(raw.rglob("*.part"))


def test_second_run_uses_cache_without_network(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    site.stop()  # мережі більше немає

    paths = fetch_all(sources, lock, raw)

    assert paths["katottg"].read_bytes() == PAYLOAD_A


def test_tampered_cache_raises_checksum_mismatch(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    (raw / "katottg" / "a.xlsx").write_bytes(b"tampered")

    with pytest.raises(ChecksumMismatch, match="a.xlsx"):
        fetch_all(sources, lock, raw)


def test_download_with_changed_upstream_raises_and_leaves_no_file(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    (raw / "katottg" / "a.xlsx").unlink()
    (site.root / "a.xlsx").write_bytes(b"upstream changed")

    with pytest.raises(ChecksumMismatch):
        fetch_all(sources, lock, raw)

    assert not (raw / "katottg" / "a.xlsx").exists()
    assert not list(raw.rglob("*.part"))


def test_missing_cache_is_downloaded_and_verified(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    (raw / "shapes" / "b.dbf").unlink()

    paths = fetch_all(sources, lock, raw)

    assert (raw / "shapes" / "b.dbf").read_bytes() == b"dbf"
    assert paths["shapes"].exists()


def test_missing_lock_raises_lock_error(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    with pytest.raises(LockError):
        fetch_all(sources, tmp_path / "absent.lock", tmp_path / "raw")


def test_lock_with_other_url_raises_lock_error(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    other = _write_sources(tmp_path / "other.yaml", "http://127.0.0.1:1")

    with pytest.raises(LockError, match="url"):
        fetch_all(other, lock, raw)


def test_osm_timestamp_recorded_for_flagged_source(
    site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        yaml.safe_dump(
            {
                "sources": {
                    "osm": {
                        "license": "ODbL 1.0",
                        "osm_timestamp": True,
                        "files": [{"name": "a.xlsx", "url": f"{site.base}/a.xlsx"}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    seen: list[Path] = []

    def fake_timestamp(pbf: Path) -> str:
        seen.append(pbf)
        return "2026-10-28T20:21:22Z"

    monkeypatch.setattr(fetch, "osm_timestamp", fake_timestamp)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"

    fetch_all(sources, lock, raw, update=True)

    entry = yaml.safe_load(lock.read_text(encoding="utf-8"))["sources"]["osm"]["files"]["a.xlsx"]
    assert entry["osm_timestamp"] == "2026-10-28T20:21:22Z"
    assert seen == [raw / "osm" / "a.xlsx"]


def test_user_agent_is_sent(site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    agents: list[str] = []
    original = _Quiet.do_GET

    def spy(self: _Quiet) -> None:
        agents.append(self.headers.get("User-Agent", ""))
        original(self)

    monkeypatch.setattr(_Quiet, "do_GET", spy)

    fetch_all(sources, tmp_path / "sources.lock", tmp_path / "raw", update=True)

    assert agents
    assert all(a.startswith("geo-quiz-data/0 (+https://github.com/") for a in agents)


def test_mismatch_before_rename_keeps_existing_file_and_leaves_no_part(site: Site, tmp_path: Path) -> None:
    dest = tmp_path / "raw" / "katottg" / "a.xlsx"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"previously verified")
    wrong = hashlib.sha256(b"something else").hexdigest()

    with pytest.raises(ChecksumMismatch):
        fetch._download(f"{site.base}/a.xlsx", dest, expected_sha256=wrong)

    assert dest.read_bytes() == b"previously verified"  # завантаження не підмінило файл
    assert not dest.with_name(dest.name + ".part").exists()
    assert sorted(p.name for p in dest.parent.iterdir()) == ["a.xlsx"]


def _pin_resolved_url(site: Site, lock: Path, payload: bytes) -> str:
    """Переписує lock так, ніби `url` -- рухоме `-latest`, а `resolved_url` -- знімок із цим payload."""
    (site.root / "a-snapshot.xlsx").write_bytes(payload)
    resolved = f"{site.base}/a-snapshot.xlsx"
    data = yaml.safe_load(lock.read_text(encoding="utf-8"))
    entry = data["sources"]["katottg"]["files"]["a.xlsx"]
    entry["resolved_url"] = resolved
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    lock.write_text(yaml.safe_dump(data), encoding="utf-8")
    return resolved


def test_cold_cache_downloads_resolved_url_not_moving_url(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    _pin_resolved_url(site, lock, PAYLOAD_A)
    (site.root / "a.xlsx").write_bytes(b"latest moved on")  # рухомий url уже віддає інше
    (raw / "katottg" / "a.xlsx").unlink()

    paths = fetch_all(sources, lock, raw)

    assert paths["katottg"].read_bytes() == PAYLOAD_A


def test_cold_cache_requests_resolved_url(site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    resolved = _pin_resolved_url(site, lock, PAYLOAD_A)
    (raw / "katottg" / "a.xlsx").unlink()
    requested: list[str] = []
    real_download = fetch._download

    def spy(url: str, dest: Path, expected_sha256: str | None = None) -> dict[str, object]:
        requested.append(url)
        return real_download(url, dest, expected_sha256)

    monkeypatch.setattr(fetch, "_download", spy)

    fetch_all(sources, lock, raw)

    assert requested == [resolved]


def test_update_still_downloads_moving_url_and_refreshes_lock(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    _pin_resolved_url(site, lock, PAYLOAD_A)
    (site.root / "a.xlsx").write_bytes(b"latest moved on")

    fetch_all(sources, lock, raw, update=True)

    entry = yaml.safe_load(lock.read_text(encoding="utf-8"))["sources"]["katottg"]["files"]["a.xlsx"]
    assert entry["sha256"] == hashlib.sha256(b"latest moved on").hexdigest()
    assert "resolved_url" not in entry


def test_resolved_url_with_other_content_still_fails_checksum(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    _pin_resolved_url(site, lock, PAYLOAD_A)
    (site.root / "a-snapshot.xlsx").write_bytes(b"snapshot replaced")
    (raw / "katottg" / "a.xlsx").unlink()

    with pytest.raises(ChecksumMismatch):
        fetch_all(sources, lock, raw)

    assert not (raw / "katottg" / "a.xlsx").exists()
