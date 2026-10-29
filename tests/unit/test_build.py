"""Оркестратор збірки: атомарність `dist/<версія>/` (Завдання 12)."""

from __future__ import annotations

import inspect

import logging
from pathlib import Path

import pytest

import pipeline.build as build_module
from pipeline.build import BuildError, build

VERSION = "2026.11.1"


def _fake_produce(partial: Path, version: str) -> None:
    (partial / "file.txt").write_text(version, encoding="utf-8")


def _failing_produce(partial: Path, version: str) -> None:
    (partial / "half.txt").write_text("half", encoding="utf-8")
    raise RuntimeError("збій кроку")


def test_build_publishes_partial_dir_as_final_and_removes_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_module, "_produce", _fake_produce)

    final = build(VERSION, dist_root=tmp_path)

    assert final == tmp_path / VERSION
    assert (final / "file.txt").read_text(encoding="utf-8") == VERSION
    assert not (tmp_path / f"{VERSION}.partial").exists()


def test_build_refuses_existing_dist_without_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_module, "_produce", _fake_produce)
    (tmp_path / VERSION).mkdir()
    (tmp_path / VERSION / "old.txt").write_text("old", encoding="utf-8")

    with pytest.raises(BuildError):
        build(VERSION, dist_root=tmp_path)

    assert (tmp_path / VERSION / "old.txt").exists()
    assert not (tmp_path / f"{VERSION}.partial").exists()


def test_build_force_replaces_existing_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_module, "_produce", _fake_produce)
    (tmp_path / VERSION).mkdir()
    (tmp_path / VERSION / "old.txt").write_text("old", encoding="utf-8")

    final = build(VERSION, dist_root=tmp_path, force=True)

    assert not (final / "old.txt").exists()
    assert (final / "file.txt").exists()


def test_failed_build_keeps_partial_for_diagnostics_and_leaves_final_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(build_module, "_produce", _failing_produce)
    (tmp_path / VERSION).mkdir()
    (tmp_path / VERSION / "old.txt").write_text("old", encoding="utf-8")

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="збій кроку"):
        build(VERSION, dist_root=tmp_path, force=True)

    assert (tmp_path / f"{VERSION}.partial" / "half.txt").exists()
    assert (tmp_path / VERSION / "old.txt").exists()
    assert any(".partial" in record.getMessage() for record in caplog.records)


def test_build_rejects_malformed_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build("../x", dist_root=tmp_path)


def test_build_clears_stale_partial_from_previous_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_module, "_produce", _fake_produce)
    stale = tmp_path / f"{VERSION}.partial"
    stale.mkdir()
    (stale / "stale.txt").write_text("stale", encoding="utf-8")

    final = build(VERSION, dist_root=tmp_path)

    assert not (final / "stale.txt").exists()


def test_produce_clips_peaks_by_the_outline_before_layers_and_files() -> None:
    """Вершини відкидаються тим самим контуром, що й вода, до шарів і до файлів газетира (метрика `peak_outside`)."""
    source = inspect.getsource(build_module._produce)
    clip = source.index("drop_peaks_outside(gazetteer, outline)")
    assert clip < source.index("build_layers(")
    assert clip < source.index("_gazetteer_files(")
