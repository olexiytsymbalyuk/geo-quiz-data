"""Сторож правила 2.1: чотири неукраїнські літери не існують у файлах репозиторію.

Єдиний виняток — `config/policy.yaml` (блок-лист екзонімів). Регекс записаний
лише `\\u`-escape-послідовностями, щоб цей файл сам не порушував правила.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# U+0401/0451, U+042B/044B, U+042A/044A, U+042D/044D
FORBIDDEN = re.compile(r"[\u0401\u0451\u042b\u044b\u042a\u044a\u042d\u044d]")
ALLOWED = {"config/policy.yaml"}
SKIP_DIRS = {".git", ".venv", "data", "dist", "__pycache__", ".mypy_cache", ".pytest_cache", ".idea"}


def _repo_files(root: Path) -> list[str]:
    """Усі файли репозиторію: tracked + untracked (без ignored); запасний обхід без git."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        names = [n for n in out.decode("utf-8").split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                names.append(str((Path(dirpath) / f).relative_to(root)))
    return sorted(n for n in set(names) if not (set(Path(n).parts) & SKIP_DIRS))


def offenders(root: Path = ROOT) -> list[str]:
    found: list[str] = []
    for name in _repo_files(root):
        if name in ALLOWED:
            continue
        path = root / name
        if not path.is_file():
            continue  # видалений, але ще не зафіксований файл
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # двійковий файл
        if FORBIDDEN.search(text):
            found.append(name)
    return found


def test_no_forbidden_glyphs_outside_policy() -> None:
    assert offenders() == []


def test_detector_catches_each_forbidden_letter(tmp_path: Path) -> None:
    for code in (0x401, 0x451, 0x42B, 0x44B, 0x42A, 0x44A, 0x42D, 0x44D):
        (tmp_path / "x.txt").write_text(f"a{chr(code)}b", encoding="utf-8")
        assert offenders(tmp_path) == ["x.txt"], hex(code)


def test_detector_ignores_ukrainian_text_and_allowed_file(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("Їжак, єдність, Ґанок, ім'я", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "policy.yaml").write_text(chr(0x44B), encoding="utf-8")
    assert offenders(tmp_path) == []


def test_policy_file_is_scanned_by_walker_but_exempt() -> None:
    assert "config/policy.yaml" in _repo_files(ROOT)
