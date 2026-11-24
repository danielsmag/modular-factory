"""The workspace file: which checkouts are listed, and where that list lives."""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

from ..util import Refused, find_repo, write_atomic
from .concurrency import QueueRow, parse_concurrency
from .jobs import build_snapshot
from .paths import FILE, HEADER, LEGACY_FILE, SCHEMA, migrate_user_file, toml_string, user_file
from .project import Project, labels_for
from .queue import collect_queue


class Workspace:
    def __init__(self, file: Path, projects: list[Project], concurrency: int | None = None) -> None:
        self.file: Path = file
        self.projects: list[Project] = projects
        self.concurrency: int | None = concurrency

    def label(self, project: Project) -> str:
        return labels_for(self.projects)[project.path]

    def project(self, label: str) -> Project | None:
        for item in self.projects:
            if self.label(item) == label:
                return item
        return None

    @staticmethod
    def init(directory: Path) -> Path:
        file: Path = directory / FILE
        if file.exists():
            raise Refused(f"{file} already exists")
        workspace: Workspace = Workspace(file, [])
        workspace.save()
        return file

    @staticmethod
    def find(start: Path | None = None) -> Workspace:
        return Workspace.locate(start, create=False)

    @staticmethod
    def locate(start: Path | None = None, create: bool = False) -> Workspace:
        """The workspace in effect: FACTORY_WORKSPACE, then a workspace.toml above here, then the user file.

        `create` writes the user file when the first two are absent. It never creates a file
        for a missing FACTORY_WORKSPACE path.
        """
        override: str | None = os.environ.get("FACTORY_WORKSPACE")
        if override:
            file: Path = Path(override).expanduser()
            if not file.is_file():
                raise Refused(f"{file} does not exist")
            return Workspace.load(file)
        cwd: Path = (start or Path.cwd()).resolve()
        for directory in [cwd, *cwd.parents]:
            candidate: Path = directory / FILE
            if candidate.is_file():
                return Workspace.load(candidate)
        moved: Path | None = migrate_user_file()
        if moved is not None:
            print(f"moved {LEGACY_FILE} -> {moved}", file=sys.stderr)
        user: Path = user_file()
        if user.is_file():
            return Workspace.load(user)
        if create:
            user.parent.mkdir(parents=True, exist_ok=True)
            created: Workspace = Workspace(user, [])
            created.save()
            return created
        raise Refused("no workspace. Run `mfactory workspace add` inside a project.")

    @staticmethod
    def load(file: Path) -> Workspace:
        try:
            parsed: object = tomllib.loads(file.read_text())
        except tomllib.TOMLDecodeError as error:
            raise Refused(f"{file} is not valid TOML: {error}") from error
        if not isinstance(parsed, dict):
            raise Refused(f"{file} is not valid TOML")
        schema: object = parsed.get("schema")
        if schema != SCHEMA:
            raise Refused(f"{file} is schema {schema}; this factory reads schema {SCHEMA}")
        raw_limit: object = parsed.get("concurrency")
        limit: int | None = None if raw_limit is None else parse_concurrency(raw_limit, f"{file}: concurrency")
        raw: object = parsed.get("projects") or []
        if not isinstance(raw, list):
            raise Refused(f"{file}: projects must be a list of tables")
        projects: list[Project] = []
        for row in raw:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise Refused(f"{file}: each [[projects]] entry needs a path")
            projects.append(Project(Path(row["path"]).expanduser().resolve()))
        return Workspace(file, projects, limit)

    def save(self) -> None:
        lines: list[str] = [HEADER.rstrip("\n")]
        if self.concurrency is not None:
            lines.append(f"concurrency = {self.concurrency}")
        lines.append("")
        for project in self.projects:
            lines.append("[[projects]]")
            lines.append(f"path = {toml_string(str(project.path))}")
            lines.append("")
        write_atomic(self.file, "\n".join(lines).rstrip("\n") + "\n")

    def add(self, path: Path) -> Project:
        repo: Path = find_repo(path)
        project: Project = Project(repo)
        if not project.binary.is_file():
            raise Refused(f"{repo} has no .factory/bin/mfactory. Install the factory there first.")
        if any(existing.path == project.path for existing in self.projects):
            raise Refused(f"{repo} is already in {self.file}")
        self.projects.append(project)
        self.save()
        return project

    def remove(self, ref: str) -> Project:
        wanted: Path = Path(ref).expanduser()
        resolved: Path | None = wanted.resolve() if wanted.exists() else None
        matches: list[Project] = []
        for project in self.projects:
            same_path: bool = project.path == resolved or str(project.path) == str(wanted)
            same_name: bool = ref == self.label(project) or ref == project.path.name
            if same_path or same_name:
                matches.append(project)
        if not matches:
            raise Refused(f"no project matches {ref!r}")
        if len(matches) > 1:
            raise Refused(f"{ref!r} matches more than one project; pass the path")
        chosen: Project = matches[0]
        self.projects.remove(chosen)
        self.save()
        return chosen

    def queue(self, concurrency: int | None = None) -> tuple[list[QueueRow], list[str]]:
        return collect_queue(self.projects, self.label, self.concurrency, concurrency)

    def snapshot(self) -> dict[str, object]:
        return build_snapshot(self.projects, self.label)
