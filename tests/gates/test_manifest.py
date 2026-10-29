"""Ворота: контрольні суми, склад теки і єдина `dataset_version` у всіх артефактах."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.gates.gate_support import (
    Gazetteer,
    JsonArtifact,
    TileSet,
    lock_source_violations,
    manifest_file_violations,
    summarize,
    version_violations,
)


def test_artifacts_match_manifest(dataset_dir: Path, manifest: dict[str, Any]) -> None:
    assert manifest.get("artifacts"), "маніфест без artifacts"
    found = manifest_file_violations(dataset_dir, manifest)

    assert not found, summarize(found)


def test_dataset_version_is_the_same_everywhere(
    dataset_dir: Path, manifest: dict[str, Any], tiles: TileSet, gazetteer: Gazetteer, gazetteer_json: JsonArtifact
) -> None:
    assert tiles.metadata and gazetteer.meta and gazetteer.entities, "порожній артефакт — перевірка була б порожньою"
    versions = {
        "manifest.json": manifest.get("dataset_version"),
        "метадані pmtiles": tiles.metadata.get("dataset_version"),
        "sqlite meta": _single(row["dataset_version"] for row in gazetteer.meta),
        "sqlite entity": _single(row["dataset_version"] for row in gazetteer.entities),
        "json газетира": gazetteer_json.payload.get("dataset_version"),
    }
    metrics_path = dataset_dir / "metrics.json"
    if metrics_path.is_file():
        versions["metrics.json"] = json.loads(metrics_path.read_text(encoding="utf-8")).get("dataset_version")

    found = version_violations(versions, dataset_dir.name)

    assert not found, summarize(found)


def _single(values: Any) -> object:
    """Єдине значення або `None`, якщо значень немає чи їх кілька різних."""
    distinct = set(values)
    return next(iter(distinct)) if len(distinct) == 1 else None


def test_manifest_sources_match_lock(manifest: dict[str, Any], lock: dict[str, Any]) -> None:
    """Версії й походження джерел у маніфесті — ті, що зафіксовані в `config/sources.lock`."""
    assert manifest.get("sources"), "маніфест без sources"
    found = lock_source_violations(manifest, lock)

    assert not found, summarize(found)
