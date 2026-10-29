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


def test_inputs_never_interpolated_into_run_scripts(workflow: dict[str, Any]) -> None:
    for _, step in _all_steps(workflow):
        assert "${{ inputs." not in str(step.get("run", "")), step.get("name")
        assert "${{ github.event.inputs." not in str(step.get("run", "")), step.get("name")


def test_version_is_validated_in_every_job(workflow: dict[str, Any]) -> None:
    for job in _jobs(workflow).values():
        assert any("^[0-9]{4}" in str(step.get("run", "")) for step in job["steps"])


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
    assert release["needs"] == "build"
    assert "if" not in release or "success()" in str(release["if"])
    assert any("gh release" in str(step.get("run", "")) for step in release["steps"])
    assert not any("gh release" in str(step.get("run", "")) for step in _jobs(workflow)["build"]["steps"])


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
