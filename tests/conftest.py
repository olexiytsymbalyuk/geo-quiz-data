"""Спільні хуки pytest: docker-тести пропускаються, коли docker недоступний."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

DOCKER_INFO_TIMEOUT_S = 10
_SKIP_REASON = "docker недоступний (немає бінарника або демон не відповідає)"


def docker_available(
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> bool:
    """Чи є docker і чи відповідає демон. Відсутній образ — не причина для пропуску: його збирає CI."""
    if which("docker") is None:
        return False
    try:
        done = run(["docker", "info"], capture_output=True, timeout=DOCKER_INFO_TIMEOUT_S, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return bool(done.returncode == 0)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not any(item.get_closest_marker("docker") for item in items):
        return
    if docker_available():
        return
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if item.get_closest_marker("docker"):
            item.add_marker(skip)
