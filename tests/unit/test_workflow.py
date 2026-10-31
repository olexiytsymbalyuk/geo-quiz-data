"""Сторож ручної збірки в GitHub Actions: інваріанти безпеки й чесності воріт.

Workflow не запускається — перевіряється його структура (розбір YAML). Виняток — shell-тіла кроків, що
перевіряють, чи вільна версія, і створюють реліз: вони виконуються з підміненим `gh` у PATH (див. `Runner`).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "build.yml"
SHA_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _jobs(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = workflow["jobs"]
    return jobs


def _all_steps(workflow: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(name, step) for name, job in _jobs(workflow).items() for step in job["steps"]]


def _step_index(job: dict[str, Any], needle: str) -> int:
    """Індекс першого кроку, чий `run` містить `needle`."""
    for index, step in enumerate(job["steps"]):
        if needle in str(step.get("run", "")):
            return index
    raise AssertionError(f"немає кроку з {needle!r}")


def test_trigger_is_manual_dispatch_with_version_input(workflow: dict[str, Any]) -> None:
    triggers = workflow[True] if True in workflow else workflow["on"]  # YAML 1.1 читає `on` як True
    assert set(triggers) == {"workflow_dispatch"}
    version = triggers["workflow_dispatch"]["inputs"]["version"]
    assert version["required"] is True
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d+", version["default"])


def test_no_expression_is_interpolated_into_any_run_script(workflow: dict[str, Any]) -> None:
    for _, step in _all_steps(workflow):
        assert "${{" not in str(step.get("run", "")), step.get("name")


def test_version_is_validated_once_and_gates_every_other_job(workflow: dict[str, Any]) -> None:
    jobs = _jobs(workflow)
    validate = jobs["validate"]
    assert any("^[0-9]{4}" in str(step.get("run", "")) for step in validate["steps"])
    assert "version" in validate["outputs"]
    for name, job in jobs.items():
        if name == "validate":
            continue
        assert "validate" in _as_list(job["needs"]), name
        # Версія надходить лише з виходу validate, а не з сирого входу workflow.
        assert job["env"]["VERSION"] == "${{ needs.validate.outputs.version }}", name
        assert "inputs." not in str(job), name


def _as_list(needs: str | list[str]) -> list[str]:
    return [needs] if isinstance(needs, str) else needs


def test_every_action_is_pinned_to_a_full_commit_sha(workflow: dict[str, Any]) -> None:
    uses = [step["uses"] for _, step in _all_steps(workflow) if "uses" in step]
    assert uses
    for ref in uses:
        assert SHA_PINNED.match(ref), ref


def test_default_permissions_are_read_only_and_write_is_only_in_release(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in _jobs(workflow).items():
        granted = job.get("permissions", {})
        if name == "release":
            assert granted == {"contents": "write"}
        else:
            assert "write" not in granted.values(), name


def test_gates_step_runs_without_continue_on_error_or_skip_flags(workflow: dict[str, Any]) -> None:
    build = _jobs(workflow)["build"]
    gates = build["steps"][_step_index(build, "pytest tests/gates ")]
    assert not gates.get("continue-on-error")
    assert "--dataset" in gates["run"]
    for weakening in ("--deselect", "-k ", "--ignore", "|| true", "xfail", "--co "):
        assert weakening not in gates["run"]
    for _, step in _all_steps(workflow):
        assert not step.get("continue-on-error"), step.get("name")


def _uploads(job: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [(i, s) for i, s in enumerate(job["steps"]) if str(s.get("uses", "")).startswith("actions/upload-artifact@")]


def test_dataset_artifact_is_uploaded_only_after_gates_pass(workflow: dict[str, Any]) -> None:
    """Репозиторій публічний: датасет, що не пройшов ворота, не має потрапляти в artifacts."""
    build = _jobs(workflow)["build"]
    datasets = [(i, s) for i, s in _uploads(build) if str(s["with"]["name"]).startswith("dataset-")]
    assert len(datasets) == 1
    index, step = datasets[0]
    assert index > _step_index(build, "pytest tests/gates ")
    condition = str(step.get("if", "success()"))
    assert "always()" not in condition and "failure()" not in condition and "cancelled()" not in condition


def test_failed_build_uploads_only_diagnostics_without_tiles_or_gazetteer(workflow: dict[str, Any]) -> None:
    build = _jobs(workflow)["build"]
    heavy = ("pmtiles", "sqlite", "gazetteer", "ukraine-", "data/work/extract", "data/work/layers", "data/raw")
    for _, step in _uploads(build):
        name = str(step["with"]["name"])
        if name.startswith("dataset-"):
            continue
        paths = [line.strip() for line in str(step["with"]["path"]).splitlines() if line.strip()]
        assert paths, name
        for path in paths:
            assert not any(word in path for word in heavy), (name, path)
            # Лише метрики, маніфест і звіти воріт; ніколи ціла тека збірки чи її `.partial`.
            assert path.endswith(("metrics.json", "manifest.json")) or path == "data/work/gates", (name, path)
    assert not any(str(s["with"]["name"]).startswith("partial-") for _, s in _uploads(build))


def test_diagnostics_are_uploaded_on_failure(workflow: dict[str, Any]) -> None:
    build = _jobs(workflow)["build"]
    diagnostics = [s for _, s in _uploads(build) if not str(s["with"]["name"]).startswith("dataset-")]
    assert diagnostics
    assert any("failure()" in str(s.get("if", "")) or "always()" in str(s.get("if", "")) for s in diagnostics)
    names = {path.strip().rsplit("/", 1)[-1] for s in diagnostics for path in str(s["with"]["path"]).splitlines()}
    assert {"metrics.json", "manifest.json", "gates"} <= names


def test_release_notes_carry_licence_attribution(workflow: dict[str, Any]) -> None:
    script = "\n".join(str(step.get("run", "")) for step in _jobs(workflow)["release"]["steps"])
    for needle in _attribution():
        assert needle in script, needle


def test_release_only_after_successful_build_job(workflow: dict[str, Any]) -> None:
    release = _jobs(workflow)["release"]
    assert "build" in _as_list(release["needs"])
    assert "if" not in release or "success()" in str(release["if"])
    assert any("gh release" in str(step.get("run", "")) for step in release["steps"])
    assert not any("gh release" in str(step.get("run", "")) for step in _jobs(workflow)["build"]["steps"])


def test_release_prunes_stale_assets_before_upload(workflow: dict[str, Any]) -> None:
    script = "\n".join(str(step.get("run", "")) for step in _jobs(workflow)["release"]["steps"])
    assert "--json assets" in script
    assert "gh release delete-asset" in script
    assert script.index("gh release delete-asset") < script.index("gh release upload")


def test_ram_summary_separates_python_and_docker_peaks_and_mentions_wikidata(workflow: dict[str, Any]) -> None:
    summary = next(s for s in _jobs(workflow)["build"]["steps"] if "GITHUB_STEP_SUMMARY" in str(s.get("run", "")))
    text = summary["run"]
    assert "python_peak_rss_mb" in text and "tool_max_rss_mb" in text
    assert "Docker" in text
    assert "wikidata_queried_at" in text


def test_no_apt_install_without_update(workflow: dict[str, Any]) -> None:
    for _, step in _all_steps(workflow):
        run = str(step.get("run", ""))
        assert "apt-get install" not in run or "apt-get update" in run


def test_release_is_m0_prerelease_with_clobber_and_token(workflow: dict[str, Any]) -> None:
    release = _jobs(workflow)["release"]
    script = "\n".join(str(step.get("run", "")) for step in release["steps"])
    assert "m0" in script
    assert "--prerelease" in script
    assert "--clobber" in script
    assert "github.token" in str(release["env"]["GH_TOKEN"])


def test_build_measures_peak_memory_into_step_summary(workflow: dict[str, Any]) -> None:
    build = _jobs(workflow)["build"]
    assert "/usr/bin/time -v" in build["steps"][_step_index(build, "pipeline.build")]["run"]
    summary = "\n".join(str(s.get("run", "")) for s in build["steps"])
    assert "Maximum resident set size" in summary
    assert "GITHUB_STEP_SUMMARY" in summary


def test_jobs_have_timeouts_and_workflow_has_concurrency(workflow: dict[str, Any]) -> None:
    assert workflow["concurrency"]["group"]
    for job in _jobs(workflow).values():
        assert isinstance(job["timeout-minutes"], int)
        assert job["runs-on"] == "ubuntu-latest"


def test_workflow_has_no_forbidden_letters() -> None:
    assert not re.search("[\u0401\u0451\u042b\u044b\u042a\u044a\u042d\u044d]", WORKFLOW.read_text(encoding="utf-8"))


LOCK_KEY = "raw-${{ hashFiles('config/sources.lock') }}"


def _named(build: dict[str, Any], uses_prefix: str) -> list[dict[str, Any]]:
    return [s for s in build["steps"] if str(s.get("uses", "")).startswith(uses_prefix)]


def test_raw_cache_key_stays_on_the_lock_and_never_on_the_run(workflow: dict[str, Any]) -> None:
    build = _jobs(workflow)["build"]
    (restore,) = _named(build, "actions/cache/restore@")
    full_saves = [s for s in _named(build, "actions/cache/save@") if s["with"]["key"] == LOCK_KEY]

    assert restore["with"]["key"] == LOCK_KEY
    assert restore["with"]["path"] == "data/raw"
    assert len(full_saves) == 1


def test_complete_cache_is_saved_only_after_fetch_succeeds(workflow: dict[str, Any]) -> None:
    build = _jobs(workflow)["build"]
    (full,) = [s for s in _named(build, "actions/cache/save@") if s["with"]["key"] == LOCK_KEY]

    assert "always()" not in str(full.get("if", ""))
    assert "failure()" not in str(full.get("if", ""))
    assert "cache-hit != 'true'" in str(full["if"])
    assert build["steps"].index(full) > _step_index(build, "pipeline.fetch")


def test_partial_cache_is_saved_when_fetch_fails_without_part_files(workflow: dict[str, Any]) -> None:
    build = _jobs(workflow)["build"]
    fetch_index = _step_index(build, "pipeline.fetch")
    fetch_id = build["steps"][fetch_index]["id"]
    (partial,) = [s for s in _named(build, "actions/cache/save@") if s["with"]["key"] != LOCK_KEY]
    save_index = build["steps"].index(partial)
    condition = str(partial["if"])

    assert f"steps.{fetch_id}.outcome == 'failure'" in condition
    assert "!cancelled()" in condition  # не always(): скасований запуск може лишити недокачане
    assert save_index > fetch_index
    # ключ частково заповненого кешу не збігається з повним: той незмінний і не можна дозаписати
    assert partial["with"]["key"].startswith("raw-${{ hashFiles('config/sources.lock') }}-partial-")
    assert partial["with"]["path"] == "data/raw"
    cleanup = [i for i, s in enumerate(build["steps"]) if ".part" in str(s.get("run", ""))]
    assert cleanup and fetch_index < cleanup[0] < save_index
    assert f"steps.{fetch_id}.outcome == 'failure'" in str(build["steps"][cleanup[0]]["if"])


def test_restore_falls_back_to_the_latest_partial_cache(workflow: dict[str, Any]) -> None:
    (restore,) = _named(_jobs(workflow)["build"], "actions/cache/restore@")

    assert restore["with"]["restore-keys"].strip() == "raw-${{ hashFiles('config/sources.lock') }}-partial-"


def test_ci_does_not_download_the_moving_land_polygons_archive(workflow: dict[str, Any]) -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "osmdata" not in text
    assert "land-polygons-split-4326.zip" not in text


# --- незмінний реліз dataset-<версія> ---------------------------------------------------------------------------

VERSION = "2026.11.1"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
# Рівно ті файли, які споживач завантажує з релізу: manifest.json і все, що він перелічує в `artifacts`.
RELEASE_FILE_NAMES = (
    f"ukraine-{VERSION}.pmtiles",
    f"gazetteer-{VERSION}.sqlite",
    f"gazetteer-ukraine-{VERSION}.json",
    "manifest.json",
    "metrics.json",
)
MANIFEST_BYTES = b'{"dataset_version": "2026.11.1"}\n'
MANIFEST_SHA256 = hashlib.sha256(MANIFEST_BYTES).hexdigest()
VERSION_CHECK = 'gh release view "dataset-$VERSION"'
DATASET_RELEASE = 'gh release create "dataset-$VERSION"'
MOVING_PRERELEASE = "gh release create m0"


def _step(job: dict[str, Any], needle: str) -> dict[str, Any]:
    step: dict[str, Any] = job["steps"][_step_index(job, needle)]
    return step


def test_existing_dataset_release_fails_before_the_build(workflow: dict[str, Any]) -> None:
    jobs = _jobs(workflow)
    validate = jobs["validate"]

    assert validate["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert validate["env"]["GH_REPO"] == "${{ github.repository }}"
    check = _step(validate, VERSION_CHECK)
    assert "exit 1" in check["run"]
    assert "if" not in check  # перевірку не можна обійти умовою
    assert _step_index(validate, "^[0-9]{4}") < _step_index(validate, VERSION_CHECK)  # до gh доходить лише версія з правильним форматом
    # build залежить від validate, тож двогодинна збірка не стартує, поки перевірка червона
    assert "validate" in _as_list(jobs["build"]["needs"])


def test_dataset_release_is_created_once_and_never_clobbered(workflow: dict[str, Any]) -> None:
    release = _jobs(workflow)["release"]
    creating = [step for step in release["steps"] if DATASET_RELEASE in str(step.get("run", ""))]

    assert len(creating) == 1
    script = creating[0]["run"]
    assert '"${files[@]}"' in script
    assert '--target "$GITHUB_SHA"' in script
    for forbidden in ("--clobber", "gh release upload", "delete-asset", "--prerelease"):
        assert forbidden not in script, forbidden
    # Створений реліз версії жоден крок workflow потім не змінює.
    for _, step in _all_steps(workflow):
        assert not re.search(r'gh release (edit|upload|delete|delete-asset) +"?dataset-', str(step.get("run", ""))), step.get("name")


def test_dataset_release_precedes_the_moving_prerelease(workflow: dict[str, Any]) -> None:
    release = _jobs(workflow)["release"]

    assert _step_index(release, DATASET_RELEASE) < _step_index(release, MOVING_PRERELEASE)


def test_manifest_sha_goes_to_the_summary(workflow: dict[str, Any]) -> None:
    script = _step(_jobs(workflow)["release"], DATASET_RELEASE)["run"]

    assert re.search(r'sha256sum +"?[^\s"]*manifest\.json', script)
    assert "GITHUB_STEP_SUMMARY" in script


def test_both_releases_read_one_file_list_and_one_set_of_notes(workflow: dict[str, Any]) -> None:
    release = _jobs(workflow)["release"]

    assert len([step for step in release["steps"] if ".pmtiles" in str(step.get("run", ""))]) == 1  # імена файлів — в одному місці
    for marker in (DATASET_RELEASE, MOVING_PRERELEASE):
        script = _step(release, marker)["run"]
        assert "release-files.txt" in script and "release-notes.md" in script, marker
        assert ".pmtiles" not in script, marker


# --- ті самі кроки, виконані з підміненим gh -------------------------------------------------------------------

# Кожен виклик gh пишеться в журнал як аргументи, що завершуються \037, і \036 наприкінці: нотатки багаторядкові.
GH_STUB_HEAD = r"""#!/bin/bash
{ printf '%s\037' "$@"; printf '\036'; } >> "$STUB_LOG"
"""


@dataclass(frozen=True)
class GhRule:
    """Відповідь заглушки gh на виклик, чиї аргументи (через пробіл) починаються з `prefix`; перше правило виграє."""

    prefix: str
    status: int = 0
    out: str = ""
    err: str = ""


def _gh_stub(rules: Sequence[GhRule]) -> str:
    branches = "".join(
        f'if [[ "$*" == {shlex.quote(rule.prefix)}* ]]; then\n'
        f"  printf '%s' {shlex.quote(rule.out)}\n"
        f"  printf '%s' {shlex.quote(rule.err)} >&2\n"
        f"  exit {rule.status}\n"
        "fi\n"
        for rule in rules
    )
    return GH_STUB_HEAD + branches + "exit 0\n"


@dataclass(frozen=True)
class Runner:
    """Імітація раннера для shell-кроків: робоча тека, RUNNER_TEMP, підсумок і підмінений `gh` попереду в PATH."""

    workspace: Path
    temp: Path
    summary: Path
    log: Path
    env: dict[str, str]

    def run(self, step: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        script = self.temp / "step.sh"
        script.write_text(step["run"], encoding="utf-8")
        # Як раннер: `bash -e {0}`, де {0} — файл із тілом кроку.
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", str(script)],
            cwd=self.workspace,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        text = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        return [record.split("\x1f")[:-1] for record in text.split("\x1e") if record]


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _runner(tmp_path: Path, *rules: GhRule) -> Runner:
    workspace, temp, bin_dir = tmp_path / "workspace", tmp_path / "runner-temp", tmp_path / "bin"
    for directory in (workspace, temp, bin_dir):
        directory.mkdir()
    _executable(bin_dir / "gh", _gh_stub(rules))
    if shutil.which("sha256sum") is None:  # немає GNU sha256sum: той самий формат виводу дає shasum
        _executable(bin_dir / "sha256sum", '#!/bin/bash\nexec shasum -a 256 "$@"\n')
    summary, log = tmp_path / "summary.md", tmp_path / "gh.log"
    # Мінімальне оточення: реальні GITHUB_* і GH_* (коли тест іде в Actions) у крок не потрапляють.
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "VERSION": VERSION,
        "GITHUB_SHA": COMMIT,
        "GITHUB_STEP_SUMMARY": str(summary),
        "GH_TOKEN": "test-token",
        "GH_REPO": "owner/repo",
        "RUNNER_TEMP": str(temp),
        "STUB_LOG": str(log),
    }
    return Runner(workspace, temp, summary, log, env)


def _write_release_files(workspace: Path, *, omit: Sequence[str] = (), extra: Sequence[str] = ()) -> None:
    """Теку `release-files/`, як її лишає download-artifact."""
    directory = workspace / "release-files"
    directory.mkdir()
    for name in (*RELEASE_FILE_NAMES, *extra):
        if name not in omit:
            (directory / name).write_bytes(MANIFEST_BYTES if name == "manifest.json" else name.encode("utf-8"))


def _run_job(runner: Runner, steps: Sequence[dict[str, Any]]) -> list[subprocess.CompletedProcess[str]]:
    """Кроки по черзі, як у job: після першого збою решта не запускається."""
    done: list[subprocess.CompletedProcess[str]] = []
    for step in steps:
        done.append(runner.run(step))
        if done[-1].returncode != 0:
            break
    return done


def _release_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Shell-кроки job release у порядку запуску: перелік файлів і нотаток, реліз версії, m0."""
    release = _jobs(workflow)["release"]
    return [_step(release, ".pmtiles"), _step(release, DATASET_RELEASE), _step(release, MOVING_PRERELEASE)]


def _option(call: Sequence[str], flag: str) -> str:
    return call[call.index(flag) + 1]


def _attribution() -> tuple[str, ...]:
    from pipeline.tiles import KATOTTG_LICENSOR

    return ("OpenStreetMap contributors", "ODbL 1.0", KATOTTG_LICENSOR, "CC BY 4.0", "Natural Earth", "Wikidata", "CC0")


def test_version_check_blocks_a_release_that_already_exists(workflow: dict[str, Any], tmp_path: Path) -> None:
    runner = _runner(tmp_path, GhRule("release view dataset-", out="title: Атлас Україна\n"))

    done = runner.run(_step(_jobs(workflow)["validate"], VERSION_CHECK))

    assert done.returncode == 1
    assert "dataset-2026.11.1 уже існує" in done.stdout
    assert runner.calls() == [["release", "view", "dataset-2026.11.1"]]


def test_version_check_passes_when_gh_says_release_not_found(workflow: dict[str, Any], tmp_path: Path) -> None:
    runner = _runner(tmp_path, GhRule("release view dataset-", status=1, err="release not found\n"))

    done = runner.run(_step(_jobs(workflow)["validate"], VERSION_CHECK))

    assert done.returncode == 0, done.stdout + done.stderr


# gh завершується кодом 1 і на «не знайдено», і на помилку авторизації чи мережі: відрізнити їх можна лише за текстом.
# Перші два тексти — дослівний вивід gh 2.100.0 (токен хибний; хост недосяжний).
@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param(
            "HTTP 401: Bad credentials (https://api.github.com/repos/owner/repo/releases/tags/dataset-2026.11.1)\n"
            "Try authenticating with:  gh auth login -h github.com\n",
            id="auth",
        ),
        pytest.param("error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com\n", id="network"),
        pytest.param("HTTP 502: Bad Gateway (https://api.github.com/repos/owner/repo/releases/tags/dataset-2026.11.1)\n", id="server"),
        pytest.param("", id="silent"),
        pytest.param("release not found, mirror is lagging\n", id="not-found-with-extra-text"),
    ],
)
def test_version_check_fails_closed_on_any_other_gh_error(workflow: dict[str, Any], tmp_path: Path, stderr: str) -> None:
    runner = _runner(tmp_path, GhRule("release view dataset-", status=1, err=stderr))

    done = runner.run(_step(_jobs(workflow)["validate"], VERSION_CHECK))

    assert done.returncode == 1
    assert "не вдалося перевірити" in done.stdout
    assert "уже існує" not in done.stdout


def test_files_step_lists_the_five_files_and_writes_notes_with_attribution(workflow: dict[str, Any], tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    _write_release_files(runner.workspace)

    done = runner.run(_release_steps(workflow)[0])

    assert done.returncode == 0, done.stdout + done.stderr
    assert (runner.temp / "release-files.txt").read_text(encoding="utf-8").splitlines() == [f"release-files/{n}" for n in RELEASE_FILE_NAMES]
    notes = (runner.temp / "release-notes.md").read_text(encoding="utf-8")
    assert VERSION in notes and COMMIT in notes
    for needle in _attribution():
        assert needle in notes, needle


@pytest.mark.parametrize("missing", RELEASE_FILE_NAMES)
def test_files_step_fails_when_a_listed_file_is_missing(workflow: dict[str, Any], tmp_path: Path, missing: str) -> None:
    runner = _runner(tmp_path)
    _write_release_files(runner.workspace, omit=(missing,))

    done = runner.run(_release_steps(workflow)[0])

    assert done.returncode == 1
    assert missing in done.stdout
    assert not (runner.temp / "release-files.txt").exists()


def test_files_step_fails_on_a_file_the_release_would_leave_out(workflow: dict[str, Any], tmp_path: Path) -> None:
    """Реліз незмінний: файл, який маніфест перелічив би, а перелік не знає, не можна буде додати потім."""
    runner = _runner(tmp_path)
    _write_release_files(runner.workspace, extra=("extra-2026.11.1.json",))

    done = runner.run(_release_steps(workflow)[0])

    assert done.returncode == 1
    assert "extra-2026.11.1.json" in done.stdout
    assert not (runner.temp / "release-files.txt").exists()


def test_dataset_release_is_created_with_the_listed_files_the_build_commit_and_the_manifest_sha(
    workflow: dict[str, Any], tmp_path: Path
) -> None:
    runner = _runner(tmp_path)
    _write_release_files(runner.workspace)
    files_step, dataset_step, _ = _release_steps(workflow)

    done = _run_job(runner, [files_step, dataset_step])

    assert [d.returncode for d in done] == [0, 0], [d.stdout + d.stderr for d in done]
    (call,) = runner.calls()
    assert call[:3] == ["release", "create", "dataset-2026.11.1"]
    assert call[3:8] == [f"release-files/{n}" for n in RELEASE_FILE_NAMES]
    assert _option(call, "--target") == COMMIT
    assert "--clobber" not in call and "--prerelease" not in call
    notes = _option(call, "--notes")
    assert MANIFEST_SHA256 in notes
    for needle in _attribution():
        assert needle in notes, needle
    summary = runner.summary.read_text(encoding="utf-8")
    assert MANIFEST_SHA256 in summary and "dataset-2026.11.1" in summary


def test_dataset_release_is_not_created_when_the_manifest_sha_is_not_a_sha256(workflow: dict[str, Any], tmp_path: Path) -> None:
    """Нотатки незмінного релізу не можуть нести сміття замість суми, яку закріплюють у споживача."""
    runner = _runner(tmp_path)
    _write_release_files(runner.workspace)
    _executable(tmp_path / "bin" / "sha256sum", "#!/bin/bash\necho 'SHA256 (release-files/manifest.json) = not-a-hash'\n")
    files_step, dataset_step, _ = _release_steps(workflow)

    done = _run_job(runner, [files_step, dataset_step])

    assert [d.returncode for d in done] == [0, 1]
    assert "sha256" in done[-1].stdout
    assert runner.calls() == []


def test_failed_creation_leaves_the_moving_prerelease_and_the_summary_untouched(workflow: dict[str, Any], tmp_path: Path) -> None:
    runner = _runner(tmp_path, GhRule("release create dataset-", status=1, err="HTTP 422: Validation Failed (tag_name already exists)\n"))
    _write_release_files(runner.workspace)

    done = _run_job(runner, _release_steps(workflow))

    assert [d.returncode != 0 for d in done] == [False, True]  # перелік файлів — так, реліз — ні, m0 не запускався
    assert [call[:3] for call in runner.calls()] == [["release", "create", "dataset-2026.11.1"]]
    assert not runner.summary.exists() or runner.summary.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("m0_exists", [True, False], ids=["m0-exists", "m0-absent"])
def test_moving_prerelease_reuses_the_files_and_notes_and_prunes_stale_assets(workflow: dict[str, Any], tmp_path: Path, m0_exists: bool) -> None:
    stale = "ukraine-2026.10.1.pmtiles"
    runner = _runner(
        tmp_path,
        GhRule("release view m0 --json assets", out=f"{stale}\nmetrics.json\n"),
        GhRule("release view m0", status=0 if m0_exists else 1, err="" if m0_exists else "release not found\n"),
    )
    _write_release_files(runner.workspace)

    done = _run_job(runner, _release_steps(workflow))

    assert [d.returncode for d in done] == [0, 0, 0], [d.stdout + d.stderr for d in done]
    calls = runner.calls()
    created = calls[0]
    assert created[:3] == ["release", "create", "dataset-2026.11.1"]  # реліз версії — перший виклик gh у job
    m0_write = next(c for c in calls if c[:3] == ["release", "edit" if m0_exists else "create", "m0"])
    assert "--prerelease" in m0_write
    for needle in _attribution():
        assert needle in _option(m0_write, "--notes"), needle
    upload = next(c for c in calls if c[:3] == ["release", "upload", "m0"])
    assert upload[3:-1] == created[3:8]  # m0 отримує ті самі п'ять файлів
    assert upload[-1] == "--clobber"
    assert [c for c in calls if c[:2] == ["release", "delete-asset"]] == [["release", "delete-asset", "m0", stale, "--yes"]]
