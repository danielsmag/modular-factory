"""The PyPI wheel installs a `mfactory` command that can install a project."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT


def test_wheel_installs_a_command_that_installs_a_project(world, tmp_path: Path) -> None:
    venv: Path = tmp_path / "venv"
    created: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True)
    assert created.returncode == 0, created.stderr
    python: Path = venv / "bin" / "python"
    installed: subprocess.CompletedProcess[str] = subprocess.run(
        [str(python), "-m", "pip", "install", str(ROOT)], capture_output=True, text=True)
    assert installed.returncode == 0, installed.stderr
    binary: Path = venv / "bin" / "mfactory"
    assert binary.is_file()
    version: subprocess.CompletedProcess[str] = subprocess.run(
        [str(binary), "version"], capture_output=True, text=True)
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == (ROOT / "core" / "VERSION").read_text().strip()
    # The console script execs share/modular-factory/core/bin/mfactory.
    tree: Path = venv / "share" / "modular-factory" / "core" / "bin" / "mfactory"
    assert tree.is_file()
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [str(binary), "install", "--tool", "cursor"],
        cwd=world.repo, capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode == 0, result.stderr + result.stdout
    assert (world.repo / ".factory" / "bin" / "mfactory").is_file()
    assert (world.repo / ".factory" / "VERSION").read_text().strip() == version.stdout.strip()
