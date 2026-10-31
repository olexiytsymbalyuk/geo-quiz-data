"""Політика топонімів (`config/policy.yaml`) і правила версії/шляхів."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.paths import CONFIG, RAW, ROOT, WORK, dist_dir, validate_version
from pipeline.policy import RENAME_KINDS, Policy, Rename, load_policy

# Заборонені літери — лише escape-послідовностями (правило 2.1).
_FORBIDDEN = re.compile(r"[\u0401\u0451\u042b\u044b\u042a\u044a\u042d\u044d]")


def test_policy_has_rules() -> None:
    p = load_policy()
    assert p.neutral_countries == {"RU", "BY"}
    assert p.named_neighbours == {"PL", "SK", "HU", "RO", "MD"}
    assert p.startable_min == 10
    assert p.blocked_sentinel == "zz-blocked-test-form"
    assert p.blocked_sentinel in p.exonym_blocklist
    assert len(p.exonym_blocklist) >= 21  # 10+ пар із брифу + сентинел
    assert {r.new for r in p.renames_check} >= {
        "Дніпро",
        "Кропивницький",
        "Бахмут",
        "Чорноморськ",
        "Шептицький",
        "Самар",
        "Південне",
        "Південноукраїнськ",
    }
    assert len(p.renames_check) >= 20


def test_policy_is_immutable() -> None:
    p = load_policy()
    assert isinstance(p, Policy)
    with pytest.raises(AttributeError):
        p.startable_min = 1  # type: ignore[misc]


def test_blocklist_structure() -> None:
    """Склад блок-листа лише за структурою: самих російських форм поза policy.yaml немає."""
    p = load_policy()
    forms = set(p.exonym_blocklist)
    cyrillic = {f for f in forms if re.search(r"[\u0400-\u04ff]", f)}
    latin = {f for f in forms if re.fullmatch(r"[A-Za-z-]+", f)} - {p.blocked_sentinel}
    assert len(cyrillic) >= 11
    assert len(latin) >= 10
    assert p.blocked_sentinel in forms


def test_blocklist_has_no_duplicates_or_blanks() -> None:
    forms = load_policy().exonym_blocklist
    assert all(f.strip() == f and f for f in forms)
    assert len(forms) == len(set(forms))


def test_renames_are_well_formed() -> None:
    renames = load_policy().renames_check
    assert all(isinstance(r, Rename) for r in renames)
    for r in renames:
        assert r.old and r.new and r.old != r.new
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.date), r
        assert r.source.strip(), r
        # Колишні й нові назви тут — українські: жодної з чотирьох заборонених літер.
        assert not _FORBIDDEN.search(r.old), r
        assert not _FORBIDDEN.search(r.new), r
    assert len({(r.old, r.new) for r in renames}) == len(renames)


def test_renames_cover_each_wave() -> None:
    by_new = {r.new: r for r in load_policy().renames_check}
    assert by_new["Дніпро"].old == "Дніпропетровськ"
    assert by_new["Кропивницький"].old == "Кіровоград"
    assert by_new["Бахмут"].old == "Артемівськ"
    assert by_new["Чорноморськ"].old == "Іллічівськ"
    assert by_new["Шептицький"].old == "Червоноград"
    assert by_new["Самар"].old == "Новомосковськ"
    assert by_new["Південне"].old == "Южне"
    assert by_new["Південноукраїнськ"].old == "Южноукраїнськ"
    assert by_new["Шептицький"].date == "2024-09-19"
    assert by_new["Південне"].date == "2024-10-09"
    assert by_new["Дніпро"].date.startswith("2016-")


def test_renames_have_kind_and_optional_katottg() -> None:
    renames = load_policy().renames_check
    assert all(r.kind in RENAME_KINDS for r in renames)
    by_new = {r.new: r for r in renames}
    assert by_new["Дніпро"].kind == "soviet"
    assert by_new["Шептицький"].kind == "imperial"
    assert all(r.katottg is None or re.fullmatch(r"UA\d{17}", r.katottg) for r in renames)


def test_homonym_exceptions_are_codes_of_old_names() -> None:
    p = load_policy()
    assert len(p.renames_homonym_exceptions) == 7  # Красний Лиман ×2, Красний Луч ×2, Ватутіне, Южне ×2 (R42)
    assert all(re.fullmatch(r"UA\d{17}", code) for code in p.renames_homonym_exceptions)


def test_load_policy_rejects_bad_kind_and_bad_exception_code(tmp_path: Path) -> None:
    base = (CONFIG / "policy.yaml").read_text(encoding="utf-8")
    bad_kind = tmp_path / "a.yaml"
    bad_kind.write_text(base.replace("kind: soviet", "kind: tsarist", 1), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(bad_kind)
    bad_code = tmp_path / "b.yaml"
    bad_code.write_text(
        base + "\nrenames_homonym_exceptions:\n  - {katottg: UA1, name: Южне, note: x}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_policy(bad_code)


def test_homonym_exception_must_name_an_old_form_and_not_echo_it(tmp_path: Path) -> None:
    base = (CONFIG / "policy.yaml").read_text(encoding="utf-8")
    stray = tmp_path / "c.yaml"
    stray.write_text(
        base + "\nrenames_homonym_exceptions:\n  - {katottg: UA01100030020040197, name: Вигадане, note: x}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="№1") as excinfo:
        load_policy(stray)
    assert "Вигадане" not in str(excinfo.value)


def test_load_policy_rejects_missing_keys(tmp_path: Path) -> None:
    bad = tmp_path / "policy.yaml"
    bad.write_text("neutral_countries: [RU]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(bad)


def test_version_format() -> None:
    assert validate_version("2026.11.1") == "2026.11.1"
    assert validate_version("2026.01.12") == "2026.01.12"
    for bad in ("2026.13.1", "2026.00.1", "2026.11", "26.11.1", "2026.11.1.0", "v2026.11.1", "2026.11.x", ""):
        with pytest.raises(ValueError):
            validate_version(bad)


def test_paths() -> None:
    assert (ROOT / "pyproject.toml").is_file()
    assert CONFIG == ROOT / "config"
    assert RAW == ROOT / "data" / "raw"
    assert WORK == ROOT / "data" / "work"
    assert dist_dir("2026.11.1") == ROOT / "dist" / "2026.11.1"
    with pytest.raises(ValueError):
        dist_dir("../etc")
