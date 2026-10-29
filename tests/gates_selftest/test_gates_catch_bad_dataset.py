"""Самотест воріт: «добрий» крихітний датасет проходить усі ворота, «поганий» валить саме свої.

Без цього не відрізнити «ворота ловлять порушення» від «ворота падають завжди».
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.paths import ROOT
from pipeline.text import FORBIDDEN_RE
from pipeline.policy import load_policy
from tests.gates_selftest.bad_dataset import FORBIDDEN_SAMPLE, VIOLATIONS, build_dataset

GATES_DIR = ROOT / "tests" / "gates"
GATE_FILES = (
    "test_no_name_ru_key",
    "test_no_russian_letters",
    "test_exonym_blocklist",
    "test_no_neutral_country",
    "test_manifest",
)
SUBPROCESS_TIMEOUT_S = 120
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_USAGE_ERROR = 4


def run_gates(target: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Запускає ворота як окремий pytest, як це робить CI."""
    command = [sys.executable, "-m", "pytest", target, "-q", "-p", "no:cacheprovider", *args]
    return subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_S, check=False
    )


def test_every_gate_file_has_a_violation() -> None:
    assert set(VIOLATIONS.values()) == set(GATE_FILES)
    assert all((GATES_DIR / f"{name}.py").is_file() for name in GATE_FILES)


def test_good_dataset_passes_all_gates(tmp_path: Path) -> None:
    dataset = build_dataset(tmp_path)

    done = run_gates("tests/gates", "--dataset", str(dataset))

    assert done.returncode == EXIT_OK, done.stdout[-2000:]
    assert " passed" in done.stdout and " failed" not in done.stdout


@pytest.mark.parametrize("violation", sorted(VIOLATIONS))
def test_gate_fails_on_its_own_violation(violation: str, tmp_path: Path) -> None:
    dataset = build_dataset(tmp_path, violation)
    gate = VIOLATIONS[violation]

    done = run_gates(f"tests/gates/{gate}.py", "--dataset", str(dataset))

    assert done.returncode == EXIT_TESTS_FAILED, done.stdout[-2000:]


@pytest.mark.parametrize("violation", sorted(VIOLATIONS))
def test_failure_output_never_echoes_the_offending_text(violation: str, tmp_path: Path) -> None:
    dataset = build_dataset(tmp_path, violation)
    done = run_gates(f"tests/gates/{VIOLATIONS[violation]}.py", "--dataset", str(dataset))
    output = done.stdout + done.stderr

    assert FORBIDDEN_RE.search(output) is None
    assert FORBIDDEN_SAMPLE not in output
    assert load_policy().blocked_sentinel not in output


def test_gates_without_dataset_option_are_a_usage_error_not_a_skip() -> None:
    done = run_gates("tests/gates")

    assert done.returncode == EXIT_USAGE_ERROR
    assert "--dataset" in done.stdout + done.stderr
