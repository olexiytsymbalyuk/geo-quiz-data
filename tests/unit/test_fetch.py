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
    def do_GET(self) -> None:  # noqa: N802
        """`/same/<файл>` -- редирект на тому ж хості, `/other/<файл>` -- на `localhost` (інший хост, як підписаний URL GitHub)."""
        route, _, name = self.path.lstrip("/").partition("/")
        if route in ("same", "other") and name:
            host = f"127.0.0.1:{self.server.server_address[1]}" if route == "same" else f"localhost:{self.server.server_address[1]}"
            self.send_response(302)
            self.send_header("Location", f"http://{host}/{name}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

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


def test_tampered_cache_is_replaced_by_a_fresh_verified_download(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    (raw / "katottg" / "a.xlsx").write_bytes(b"tampered")

    paths = fetch_all(sources, lock, raw)

    assert paths["katottg"].read_bytes() == PAYLOAD_A
    assert not list(raw.rglob("*.part"))


def test_tampered_cache_still_raises_when_the_fresh_download_mismatches_too(site: Site, tmp_path: Path) -> None:
    sources = _write_sources(tmp_path / "sources.yaml", site.base)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    fetch_all(sources, lock, raw, update=True)
    (raw / "katottg" / "a.xlsx").write_bytes(b"tampered")
    (site.root / "a.xlsx").write_bytes(b"upstream changed")

    with pytest.raises(ChecksumMismatch, match="a.xlsx"):
        fetch_all(sources, lock, raw)

    assert not (raw / "katottg" / "a.xlsx").exists()
    assert not list(raw.rglob("*.part"))


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


DERIVED_FROM = {
    "url": "https://example.invalid/land-polygons-split-4326.zip",
    "sha256": "f" * 64,
    "size": 928_624_032,
    "dataset_bbox": [22.0, 44.0, 40.5, 52.5],
    "clip_margin_deg": 3.0,
}


def _write_derived_sources(path: Path, base: str, derived: dict[str, object] | None, route: str = "") -> Path:
    item: dict[str, object] = {"name": "a.xlsx", "url": f"{base}/{route}a.xlsx"}
    if derived is not None:
        item["derived_from"] = derived
    path.write_text(
        yaml.safe_dump({"sources": {"land": {"license": "ODbL 1.0", "files": [item]}}}), encoding="utf-8"
    )
    return path


def _write_derived_lock(lock: Path, base: str, derived: dict[str, object] | None, route: str = "") -> dict[str, object]:
    """Lock так, як його пише `land_clip --write-lock`: запис land з походженням, sha256 -- від PAYLOAD_A."""
    entry: dict[str, object] = {
        "url": f"{base}/{route}a.xlsx",
        "fetched_at": "2026-10-29T00:00:00Z",
        "sha256": hashlib.sha256(PAYLOAD_A).hexdigest(),
        "size": len(PAYLOAD_A),
    }
    if derived is not None:
        entry["derived_from"] = derived
    lock.write_text(yaml.safe_dump({"sources": {"land": {"license": "ODbL 1.0", "files": {"a.xlsx": entry}}}}), encoding="utf-8")
    return entry


def _lock_entry(lock: Path) -> dict[str, object]:
    entry: dict[str, object] = yaml.safe_load(lock.read_text(encoding="utf-8"))["sources"]["land"]["files"]["a.xlsx"]
    return entry


def test_update_leaves_a_derived_entry_exactly_as_land_clip_wrote_it(site: Site, tmp_path: Path) -> None:
    sources = _write_derived_sources(tmp_path / "sources.yaml", site.base, DERIVED_FROM, "other/")
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    written = _write_derived_lock(lock, site.base, DERIVED_FROM, "other/")

    fetch_all(sources, lock, raw, update=True)

    assert _lock_entry(lock) == written  # ні resolved_url, ні нового fetched_at
    assert (raw / "land" / "a.xlsx").read_bytes() == PAYLOAD_A  # файл при цьому перевірено й закешовано


def test_update_of_a_derived_entry_without_a_lock_entry_points_to_land_clip(site: Site, tmp_path: Path) -> None:
    sources = _write_derived_sources(tmp_path / "sources.yaml", site.base, DERIVED_FROM)

    with pytest.raises(LockError, match="land_clip"):
        fetch_all(sources, tmp_path / "sources.lock", tmp_path / "raw", update=True)


def test_update_of_a_derived_entry_with_stale_provenance_is_a_lock_error(site: Site, tmp_path: Path) -> None:
    sources = _write_derived_sources(tmp_path / "sources.yaml", site.base, DERIVED_FROM)
    lock = tmp_path / "sources.lock"
    _write_derived_lock(lock, site.base, {**DERIVED_FROM, "sha256": "e" * 64})

    with pytest.raises(LockError, match="derived_from"):
        fetch_all(sources, lock, tmp_path / "raw", update=True)


def test_update_of_a_derived_entry_rejects_a_file_that_does_not_match_the_lock(site: Site, tmp_path: Path) -> None:
    sources = _write_derived_sources(tmp_path / "sources.yaml", site.base, DERIVED_FROM)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    _write_derived_lock(lock, site.base, DERIVED_FROM)
    (site.root / "a.xlsx").write_bytes(b"re-uploaded asset")

    with pytest.raises(ChecksumMismatch):
        fetch_all(sources, lock, raw, update=True)


def test_lock_without_derived_from_is_stale_when_sources_declare_it(site: Site, tmp_path: Path) -> None:
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    _write_derived_lock(lock, site.base, None)
    derived = _write_derived_sources(tmp_path / "derived.yaml", site.base, DERIVED_FROM)

    with pytest.raises(LockError, match="derived_from"):
        fetch_all(derived, lock, raw)


def test_changed_provenance_makes_the_lock_stale(site: Site, tmp_path: Path) -> None:
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    _write_derived_lock(lock, site.base, DERIVED_FROM)
    other = {**DERIVED_FROM, "sha256": "e" * 64}

    with pytest.raises(LockError, match="derived_from"):
        fetch_all(_write_derived_sources(tmp_path / "b.yaml", site.base, other), lock, raw)


def test_matching_provenance_uses_the_cache(site: Site, tmp_path: Path) -> None:
    sources = _write_derived_sources(tmp_path / "sources.yaml", site.base, DERIVED_FROM)
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"
    _write_derived_lock(lock, site.base, DERIVED_FROM)
    fetch_all(sources, lock, raw)
    site.stop()

    assert fetch_all(sources, lock, raw)["land"].read_bytes() == PAYLOAD_A


def test_redirect_to_another_host_is_not_recorded_as_resolved_url(site: Site, tmp_path: Path) -> None:
    sources = _write_sources_with_route(tmp_path / "sources.yaml", site.base, "other/")
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"

    fetch_all(sources, lock, raw, update=True)

    entry = yaml.safe_load(lock.read_text(encoding="utf-8"))["sources"]["land"]["files"]["a.xlsx"]
    assert "resolved_url" not in entry  # підписаний URL чужого хоста спливає за хвилини
    assert entry["sha256"] == hashlib.sha256(PAYLOAD_A).hexdigest()


def test_redirect_on_the_same_host_is_recorded_as_resolved_url(site: Site, tmp_path: Path) -> None:
    sources = _write_sources_with_route(tmp_path / "sources.yaml", site.base, "same/")
    lock, raw = tmp_path / "sources.lock", tmp_path / "raw"

    fetch_all(sources, lock, raw, update=True)

    entry = yaml.safe_load(lock.read_text(encoding="utf-8"))["sources"]["land"]["files"]["a.xlsx"]
    assert entry["resolved_url"] == f"{site.base}/a.xlsx"  # датований знімок Geofabrik лишається на своєму хості


def _write_sources_with_route(path: Path, base: str, route: str) -> Path:
    return _write_derived_sources(path, base, None, route)
