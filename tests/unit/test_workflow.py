"""Сторож ручної збірки в GitHub Actions: інваріанти безпеки й чесності воріт.

Workflow не запускається в тестах — перевіряється лише його структура (розбір YAML).
"""

from __future__ import annotations

import re
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


def test_artifacts_are_uploaded_before_gates_even_if_they_fail(workflow: dict[str, Any]) -> None:
    build = _jobs(workflow)["build"]
    uploads = [i for i, s in enumerate(build["steps"]) if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert uploads
    assert min(uploads) > _step_index(build, "pipeline.build")
    assert min(uploads) < _step_index(build, "pytest tests/gates ")


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
