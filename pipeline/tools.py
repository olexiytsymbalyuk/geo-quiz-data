"""Запуск osmium і tippecanoe в Docker-образі `geo-quiz-tools`.

Усі наступні кроки конвеєра (витяг OSM, тайли) викликають інструменти лише через
`run_tool`. Пік пам'яті береться з `/usr/bin/time` усередині контейнера, тож не залежить
від того, як Docker віддає статистику.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from pipeline.paths import ROOT

TOOLS_IMAGE = "geo-quiz-tools:1"
DOCKER_DIR = ROOT / "docker"
_RSS_MARKER = "GQ_MAX_RSS_KB"
_RSS_RE = re.compile(rf"^{_RSS_MARKER}=(\d+)\s*$", re.MULTILINE)
_TAIL_LINES = 20


@dataclass(frozen=True)
class ToolRun:
    """Результат запуску: тривалість і пік пам'яті процесу в контейнері (MB)."""

    seconds: float
    max_rss_mb: float | None


class ToolError(RuntimeError):
    """Інструмент завершився з ненульовим кодом."""

    def __init__(self, returncode: int, stderr_tail: str) -> None:
        super().__init__(f"інструмент завершився з кодом {returncode}:\n{stderr_tail}")
        self.returncode = returncode
        self.stderr_tail = stderr_tail


def docker_cmd(args: list[str], mounts: dict[Path, str], workdir: str = "/work") -> list[str]:
    """Команда `docker run --rm -v <host>:<container> … -w <workdir> <образ> <args>`."""
    cmd = ["docker", "run", "--rm"]
    for host, container in mounts.items():
        cmd += ["-v", f"{host}:{container}"]
    return [*cmd, "-w", workdir, TOOLS_IMAGE, *args]


def run_tool(args: list[str], mounts: dict[Path, str], workdir: str = "/work") -> ToolRun:
    """Запускає інструмент у контейнері; stdout/stderr інструмента передаються як є.

    Ненульовий код → `ToolError` з кодом і хвостом stderr.
    """
    timed = ["/usr/bin/time", "-f", f"{_RSS_MARKER}=%M", *args]
    started = time.monotonic()
    done = subprocess.run(docker_cmd(timed, mounts, workdir), capture_output=True, text=True, check=False)
    seconds = time.monotonic() - started

    match = _RSS_RE.search(done.stderr)
    stderr = _RSS_RE.sub("", done.stderr).rstrip("\n")
    if done.returncode != 0:
        tail = "\n".join(stderr.splitlines()[-_TAIL_LINES:])
        raise ToolError(done.returncode, tail)
    if done.stdout:
        sys.stdout.write(done.stdout)
    if stderr:
        sys.stderr.write(stderr + "\n")
    return ToolRun(seconds=seconds, max_rss_mb=int(match.group(1)) / 1024 if match else None)


def build_image() -> None:
    """Збирає образ інструментів (нативно для платформи Docker-демона)."""
    subprocess.run(["docker", "build", "-t", TOOLS_IMAGE, str(DOCKER_DIR)], check=True)


def image_exists() -> bool:
    """Чи є образ локально (`docker image inspect`)."""
    done = subprocess.run(["docker", "image", "inspect", TOOLS_IMAGE], capture_output=True, check=False)
    return done.returncode == 0


def ensure_image() -> None:
    """Збирає образ, лише якщо його ще немає."""
    if not image_exists():
        build_image()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["build"]:
        build_image()
        return 0
    sys.stderr.write("використання: python -m pipeline.tools build\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
