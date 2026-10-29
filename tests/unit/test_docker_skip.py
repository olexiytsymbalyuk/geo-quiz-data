"""Рішення про пропуск docker-тестів (tests/conftest.py)."""

from __future__ import annotations

import subprocess

from conftest import DOCKER_INFO_TIMEOUT_S, docker_available


def _run_returning(code: int):  # type: ignore[no-untyped-def]
    def run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        assert cmd == ["docker", "info"]
        assert kwargs["timeout"] == DOCKER_INFO_TIMEOUT_S
        return subprocess.CompletedProcess(cmd, code)

    return run


def test_docker_unavailable_without_the_binary() -> None:
    def run(*_a: object, **_k: object) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("без docker `docker info` не викликається")

    assert docker_available(lambda _name: None, run) is False


def test_docker_unavailable_when_the_daemon_does_not_answer() -> None:
    assert docker_available(lambda _name: "/usr/bin/docker", _run_returning(1)) is False


def test_docker_unavailable_on_timeout() -> None:
    def run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd, DOCKER_INFO_TIMEOUT_S)

    assert docker_available(lambda _name: "/usr/bin/docker", run) is False


def test_docker_available_when_info_succeeds() -> None:
    assert docker_available(lambda _name: "/usr/bin/docker", _run_returning(0)) is True


def test_missing_image_is_not_a_reason_to_skip() -> None:
    # образ збирається в CI: рішення залежить лише від доступності docker
    assert docker_available(lambda _name: "/usr/bin/docker", _run_returning(0)) is True

