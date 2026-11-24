"""One checkout the workspace knows about."""

from __future__ import annotations

import hashlib
from pathlib import Path


class Project:
    def __init__(self, path: Path) -> None:
        self.path: Path = path

    @property
    def binary(self) -> Path:
        current: Path = self.path / ".factory" / "bin" / "mfactory"
        if current.is_file():
            return current
        return self.path / ".factory" / "bin" / "factory"

    @property
    def jobs_dir(self) -> Path:
        return self.path / ".factory" / "jobs"


def labels_for(projects: list[Project]) -> dict[Path, str]:
    """Folder name, plus a short hash when two checkouts share that name."""
    names: list[str] = [project.path.name for project in projects]
    labels: dict[Path, str] = {}
    for project in projects:
        label: str = project.path.name
        if names.count(project.path.name) > 1:
            digest: str = hashlib.sha256(str(project.path).encode()).hexdigest()[:4]
            label = f"{project.path.name}-{digest}"
        labels[project.path] = label
    return labels
