"""Маніфест релізу: суми артефактів, походження джерел, версія NE з lock (Завдання 12)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from pipeline.manifest import KATOTTG_ORDER, ManifestError, ne_version_from_lock, write_manifest

QUERIED_AT = "2026-10-29T13:00:00Z"
NE_URL = "https://example.org/natural-earth-vector/v5.1.2/10m_cultural/ne_10m_admin_0_countries_ukr.shp"


def _lock_text(*, katottg_modified: str = "Tue, 07 Jul 2026 13:23:38 GMT", ne_url: str = NE_URL) -> str:
    lock: dict[str, Any] = {
        "sources": {
            "katottg": {
                "license": "CC BY 4.0",
                "files": {"k.xlsx": {"url": "https://example.org/k.xlsx", "sha256": "aa", "size": 1, "last_modified": katottg_modified}},
            },
            "osm_ukraine": {
                "license": "ODbL 1.0",
                "files": {"u.pbf": {"url": "https://example.org/u.pbf", "sha256": "bb", "size": 2, "osm_timestamp": "2026-10-28T21:18:55Z"}},
            },
            "ne_admin0_ukr": {
                "license": "Public Domain",
                "files": {"ne.shp": {"url": ne_url, "sha256": "cc", "size": 3}},
            },
        }
    }
    return yaml.safe_dump(lock, allow_unicode=True)


def _write_lock(tmp_path: Path, **kwargs: str) -> Path:
    lock = tmp_path / "sources.lock"
    lock.write_text(_lock_text(**kwargs), encoding="utf-8")
    return lock


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "a.bin").write_bytes(b"alpha")
    (dist / "metrics.json").write_text('{"x": 1}\n', encoding="utf-8")
    return dist


def test_write_manifest_records_sha256_and_size_of_every_file_except_itself(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    path = write_manifest(dist, "2026.11.1", _write_lock(tmp_path), QUERIED_AT)

    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert path == dist / "manifest.json"
    assert set(manifest["artifacts"]) == {"a.bin", "metrics.json"}
    assert manifest["artifacts"]["a.bin"] == {"sha256": hashlib.sha256(b"alpha").hexdigest(), "size": 5}
    metrics = (dist / "metrics.json").read_bytes()
    assert manifest["artifacts"]["metrics.json"] == {"sha256": hashlib.sha256(metrics).hexdigest(), "size": len(metrics)}


def test_write_manifest_has_version_provenance_and_sources_from_lock(tmp_path: Path) -> None:
    manifest = json.loads(write_manifest(_dist(tmp_path), "2026.11.1", _write_lock(tmp_path), QUERIED_AT).read_text(encoding="utf-8"))

    assert manifest["dataset_version"] == "2026.11.1"
    assert manifest["katottg_order"] == KATOTTG_ORDER == "№ 1321 від 07.07.2026"
    assert manifest["ne_version"] == "5.1.2"
    assert manifest["osm_timestamp"] == "2026-10-28T21:18:55Z"
    assert manifest["wikidata_queried_at"] == QUERIED_AT
    assert manifest["sources"]["osm_ukraine"]["files"]["u.pbf"]["sha256"] == "bb"


def test_write_manifest_rewrite_does_not_list_previous_manifest(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    lock = _write_lock(tmp_path)
    write_manifest(dist, "2026.11.1", lock, QUERIED_AT)
    manifest = json.loads(write_manifest(dist, "2026.11.1", lock, QUERIED_AT).read_text(encoding="utf-8"))
    assert "manifest.json" not in manifest["artifacts"]


def test_ne_version_is_taken_from_the_tag_in_lock_url(tmp_path: Path) -> None:
    lock = _write_lock(tmp_path, ne_url=NE_URL.replace("v5.1.2", "v9.8.7"))
    assert ne_version_from_lock(lock) == "9.8.7"


def test_ne_version_without_tag_in_url_is_an_error(tmp_path: Path) -> None:
    lock = _write_lock(tmp_path, ne_url="https://example.org/ne.shp")
    with pytest.raises(ManifestError):
        ne_version_from_lock(lock)


def test_katottg_order_date_must_match_last_modified_in_lock(tmp_path: Path) -> None:
    lock = _write_lock(tmp_path, katottg_modified="Wed, 08 Jul 2026 09:00:00 GMT")
    with pytest.raises(ManifestError):
        write_manifest(_dist(tmp_path), "2026.11.1", lock, QUERIED_AT)
