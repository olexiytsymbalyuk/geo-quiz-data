"""Вартовий замороженого набору залежностей (`requirements*.txt`).

Версії ставить `uv pip compile` (команди — у README, розділ «Залежності»). Тут
перевіряємо лише те, що ламає відтворюваність збірки: кожна залежність із
`pyproject.toml` має точну версію в обох файлах, `requirements-dev.txt` —
надмножина `requirements.txt`, і версії між файлами не розходяться. Бо CI
ставить `requirements-dev.txt`, а збірка в Docker і на GitHub Actions — `requirements.txt`:
якби вони розійшлись, тести перевіряли б не ту збірку, що випускає датасет.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize(name: str) -> str:
    """Канонічна назва пакета (PEP 503): `PyYAML` і `pyyaml` — одне."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    match = _NAME_RE.match(requirement)
    assert match, requirement
    return _normalize(match.group(1))


def _pyproject_deps() -> tuple[set[str], set[str]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    runtime = {_requirement_name(r) for r in project["dependencies"]}
    dev = {_requirement_name(r) for r in project["optional-dependencies"]["dev"]}
    return runtime, dev


def _pins(path: Path) -> dict[str, str]:
    """`назва → версія` з файлу pip-формату; кожен рядок мусить бути `==`."""
    assert path.is_file(), f"немає {path.name} — згенеруйте (README, «Залежності»)"
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        requirement = line.split(";", 1)[0].strip()
        name, _, version = requirement.partition("==")
        assert version, f"{path.name}: не точна версія: {raw!r}"
        pins[_normalize(name.split("[", 1)[0])] = version.strip()
    return pins


def test_runtime_deps_are_pinned_for_the_build() -> None:
    runtime, _ = _pyproject_deps()
    missing = runtime - _pins(REQUIREMENTS).keys()
    assert not missing, f"немає в requirements.txt: {sorted(missing)}"


def test_dev_lock_covers_dev_extras() -> None:
    runtime, dev = _pyproject_deps()
    missing = (runtime | dev) - _pins(REQUIREMENTS_DEV).keys()
    assert not missing, f"немає в requirements-dev.txt: {sorted(missing)}"


def test_dev_lock_is_a_superset_with_identical_versions() -> None:
    image = _pins(REQUIREMENTS)
    dev = _pins(REQUIREMENTS_DEV)
    not_in_dev = sorted(image.keys() - dev.keys())
    assert not not_in_dev, f"є в requirements.txt, але не в -dev: {not_in_dev}"
    differing = {name: (image[name], dev[name]) for name in image if dev[name] != image[name]}
    assert not differing, f"версії розійшлися (збірка, dev): {differing}"
