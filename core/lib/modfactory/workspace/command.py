"""`mfactory workspace`: the list of projects the dashboard and the shared queue cover."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..util import find_repo
from .concurrency import parse_concurrency
from .project import Project
from .registry import Workspace

EMPTY: str = "no projects yet. Run `mfactory workspace add` inside a project."


def workspace_command(args: argparse.Namespace) -> int:
    command: str = args.workspace_command or "list"
    if command == "init":
        file: Path = Workspace.init(Path.cwd())
        print(f"ok    {file}")
        return 0
    if command == "add":
        group: Workspace = Workspace.locate(create=True)
        target: Path = Path(args.path) if args.path else Path.cwd()
        project: Project = group.add(target)
        print(f"ok    {group.label(project)}  {project.path}")
        return 0
    group = Workspace.find()
    if command == "remove":
        ref: str = args.path if args.path else str(find_repo())
        removed: Project = group.remove(ref)
        print(f"ok    removed {removed.path}")
    elif command == "list":
        if not group.projects:
            print(EMPTY)
            return 0
        for project in group.projects:
            state: str = "ok" if project.binary.is_file() else "missing .factory/bin/mfactory"
            print(f"{group.label(project)}  {project.path}  {state}")
    elif command == "queue":
        if args.concurrency is not None:
            parse_concurrency(args.concurrency, "--concurrency")
        if not group.projects:
            print(EMPTY)
            return 0
        rows: list[tuple[str, str, str, str, str, str]]
        errors: list[str]
        rows, _metas, errors = group.queue(args.concurrency)
        for row in rows:
            print("  ".join(row))
        if not rows and not errors:
            print("nothing to do")
        for error in errors:
            print(f"mfactory: {error}", file=sys.stderr)
        return 1 if errors else 0
    return 0
