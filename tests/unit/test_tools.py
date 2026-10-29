"""Тести обгортки над Docker-образом з osmium і tippecanoe."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline import tools
from pipeline.tools import TOOLS_IMAGE, ToolError, ToolRun, docker_cmd, run_tool


def test_docker_cmd_mounts_and_image() -> None:
    assert docker_cmd(["osmium", "--version"], {Path("/a"): "/data"}) == [
        "docker", "run", "--rm", "-v", "/a:/data", "-w", "/work",
        "geo-quiz-tools:1", "osmium", "--version",
    ]  # fmt: skip
    assert TOOLS_IMAGE == "geo-quiz-tools:1"


def test_docker_cmd_without_mounts_and_custom_workdir() -> None:
    assert docker_cmd(["tippecanoe", "--version"], {}, workdir="/x") == [
        "docker", "run", "--rm", "-w", "/x", "geo-quiz-tools:1", "tippecanoe", "--version",
    ]  # fmt: skip


def _fake_completed(returncode: int, stderr: str, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_tool_parses_peak_rss_and_strips_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return _fake_completed(0, "tool warning\nGQ_MAX_RSS_KB=204800\n")

    monkeypatch.setattr(tools.subprocess, "run", fake_run)
    run = run_tool(["osmium", "--version"], {Path("/a"): "/data"})
    assert isinstance(run, ToolRun)
    assert run.max_rss_mb == pytest.approx(200.0)
    assert run.seconds >= 0
    # інструмент іде в контейнері під /usr/bin/time після образу
    cmd = seen[0]
    image_at = cmd.index(TOOLS_IMAGE)
    assert cmd[image_at + 1 : image_at + 2] == ["/usr/bin/time"]
    assert cmd[-2:] == ["osmium", "--version"]


def test_run_tool_without_marker_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _fake_completed(0, ""))
    assert run_tool(["osmium", "--version"], {}).max_rss_mb is None


def test_run_tool_nonzero_exit_raises_with_code_and_stderr_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr = "\n".join(f"line {i}" for i in range(100)) + "\nCommand exited with non-zero status 3\nGQ_MAX_RSS_KB=10\n"
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: _fake_completed(3, stderr))
    with pytest.raises(ToolError) as info:
        run_tool(["osmium", "bad"], {})
    assert info.value.returncode == 3
    assert "line 99" in str(info.value)
    assert "line 0\n" not in str(info.value)  # лише хвіст stderr
    assert "GQ_MAX_RSS_KB" not in str(info.value)


@pytest.mark.docker
def test_tools_respond() -> None:
    tools.ensure_image()
    for tool in ("osmium", "tippecanoe"):
        run = run_tool([tool, "--version"], {})
        assert run.seconds > 0
