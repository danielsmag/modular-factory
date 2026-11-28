"""The Board in a real browser, when this machine has Node and Playwright. Without them it is skipped, as in CI."""

import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import ROOT


def has_playwright() -> bool:
    node: str | None = shutil.which("node")
    if node is None:
        return False
    return subprocess.run([node, "-e", "require('playwright')"], capture_output=True).returncode == 0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(url: str, seconds: float = 30) -> None:
    deadline: float = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise AssertionError(f"the sample dashboard did not answer at {url}")


def run_sample(tmp_path: Path, script: str, *extra: str) -> subprocess.CompletedProcess[str]:
    port: int = free_port()
    server: subprocess.Popen[str] = subprocess.Popen(
        [sys.executable, str(ROOT / "tests" / "sample_dashboard.py"), "--port", str(port), *extra],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        url: str = f"http://127.0.0.1:{port}/"
        wait_for(url)
        res: subprocess.CompletedProcess[str] = subprocess.run(
            ["node", str(ROOT / "tests" / script), url],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=300,
        )
        return res
    finally:
        server.terminate()
        server.wait(timeout=10)


@pytest.mark.skipif(not has_playwright(), reason="needs node and its playwright module")
def test_the_board_works_in_a_browser(tmp_path: Path) -> None:
    res: subprocess.CompletedProcess[str] = run_sample(tmp_path, "dashboard_browser.js")
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.skipif(not has_playwright(), reason="needs node and its playwright module")
def test_the_project_filter_with_three_projects(tmp_path: Path) -> None:
    res: subprocess.CompletedProcess[str] = run_sample(
        tmp_path, "dashboard_filter_browser.js", "--projects", "3"
    )
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.skipif(not has_playwright(), reason="needs node and its playwright module")
def test_the_project_filter_with_eight_projects(tmp_path: Path) -> None:
    res: subprocess.CompletedProcess[str] = run_sample(
        tmp_path, "dashboard_filter_browser.js", "--projects", "8"
    )
    assert res.returncode == 0, res.stdout + res.stderr
