"""Ворота: у жодному артефакті немає чотирьох неукраїнських літер на рівні байтів."""

from __future__ import annotations

from pathlib import Path

from tests.gates.gate_support import (
    TileSet,
    file_letter_violations,
    metadata_letter_violations,
    sqlite_letter_violations,
    summarize,
    tile_letter_violations,
)


def test_tiles_have_no_russian_letters(tiles: TileSet) -> None:
    assert tiles.refs(), "у PMTiles немає тайлів — перевірка була б порожньою"
    found = tile_letter_violations(tiles)

    assert not found, summarize(found)


def test_pmtiles_metadata_has_no_russian_letters(tiles: TileSet) -> None:
    assert tiles.metadata, "у PMTiles немає метаданих — перевірка була б порожньою"
    found = metadata_letter_violations(tiles.metadata)

    assert not found, summarize(found)


def test_sqlite_has_no_russian_letters(dataset_dir: Path) -> None:
    found: list[str] = []
    assert list(dataset_dir.glob("*.sqlite")), "немає жодного .sqlite — перевірка була б порожньою"
    for path in sorted(dataset_dir.glob("*.sqlite")):
        found += sqlite_letter_violations(path)

    assert not found, summarize(found)


def test_text_artifacts_have_no_russian_letters(dataset_dir: Path) -> None:
    """Сирі байти кожного JSON-артефакту (газетир, метрики, маніфест) і декодовані рядки."""
    found: list[str] = []
    assert list(dataset_dir.glob("*.json")), "немає жодного .json — перевірка була б порожньою"
    for path in sorted(dataset_dir.glob("*.json")):
        found += file_letter_violations(path)

    assert not found, summarize(found)
