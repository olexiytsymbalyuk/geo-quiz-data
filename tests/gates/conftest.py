"""Фікстури воріт: опція `--dataset <тека релізу>` обов'язкова, тайли й газетир читаються один раз за сесію."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from pipeline.paths import ROOT
from pipeline.policy import load_policy
from tests.gates.gate_support import (
    MANIFEST_NAME,
    Gazetteer,
    JsonArtifact,
    PolicyView,
    TileSet,
    find_one,
    load_gazetteer,
    policy_view,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--lock", action="store", default=None, help="config/sources.lock (за замовчуванням з кореня репозиторію)")
    parser.addoption("--dataset", action="store", default=None, help="тека релізу dist/<версія>/ (обов'язково)")


def pytest_configure(config: pytest.Config) -> None:
    # Без теки релізу ворота нічого не перевіряють — це помилка запуску, а не пропуск.
    # Довге трасування друкує значення фікстур; у логах не мусить бути ні назв, ні блок-листа.
    if config.option.tbstyle == "auto":
        config.option.tbstyle = "short"
    if config.getoption("--dataset") is None:
        raise pytest.UsageError("ворота потребують --dataset <тека релізу>, наприклад --dataset dist/2026.11.1")


@pytest.fixture(scope="session")
def dataset_dir(request: pytest.FixtureRequest) -> Path:
    path = Path(request.config.getoption("--dataset")).resolve()
    if not path.is_dir():
        raise pytest.UsageError(f"--dataset: {path.name} не є текою")
    return path


@pytest.fixture(scope="session")
def lock(request: pytest.FixtureRequest) -> dict[str, Any]:
    option = request.config.getoption("--lock")
    path = Path(option).resolve() if option else ROOT / "config" / "sources.lock"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise pytest.UsageError(f"--lock: {path.name} не є відображенням")
    return loaded


@pytest.fixture(scope="session")
def policy() -> PolicyView:
    return policy_view(load_policy())


@pytest.fixture(scope="session")
def manifest(dataset_dir: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((dataset_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    return loaded


@pytest.fixture(scope="session")
def tiles(dataset_dir: Path) -> TileSet:
    return TileSet(find_one(dataset_dir, "*.pmtiles"))


@pytest.fixture(scope="session")
def gazetteer(dataset_dir: Path) -> Gazetteer:
    return load_gazetteer(find_one(dataset_dir, "*.sqlite"))


@pytest.fixture(scope="session")
def gazetteer_json(dataset_dir: Path) -> JsonArtifact:
    path = find_one(dataset_dir, "gazetteer-*.json")
    return JsonArtifact(path.name, json.loads(path.read_text(encoding="utf-8")))
