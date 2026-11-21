"""`mfactory fleet`: one queue and one dashboard over subscribed projects."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..util import find_repo
from .concurrency import parse_concurrency
from .project import Project
from .registry import Fleet
from .service import serve


def fleet_command(args: argparse.Namespace) -> int:
    command: str = args.fleet_command or "dashboard"
    if command == "init":
        file: Path = Fleet.init(Path.cwd())
        print(f"ok    {file}")
        return 0
    if command in ("subscribe", "add"):
        group: Fleet = Fleet.locate(create=True)
        target: Path = Path(args.path) if args.path else Path.cwd()
        project: Project = group.add(target)
        print(f"ok    {group.label(project)}  {project.path}")
        return 0
    group = Fleet.find()
    if command in ("unsubscribe", "remove"):
        ref: str = args.path if args.path else str(find_repo())
        removed: Project = group.remove(ref)
        print(f"ok    removed {removed.path}")
    elif command == "list":
        if not group.projects:
            print("no projects yet. Run `mfactory fleet subscribe` inside a project.")
            return 0
        for project in group.projects:
            state: str = "ok" if project.binary.is_file() else "missing .factory/bin/mfactory"
            print(f"{group.label(project)}  {project.path}  {state}")
    elif command == "queue":
        if args.concurrency is not None:
            parse_concurrency(args.concurrency, "--concurrency")
        if not group.projects:
            print("no projects yet. Run `mfactory fleet subscribe` inside a project.")
            return 0
        rows: list[tuple[str, str, str, str, str, str]]
        errors: list[str]
        rows, errors = group.queue(args.concurrency)
        for row in rows:
            print("  ".join(row))
        if not rows and not errors:
            print("nothing to do")
        for error in errors:
            print(f"mfactory: {error}", file=sys.stderr)
        return 1 if errors else 0
    elif command == "dashboard":
        serve(group.file, args.port, not args.no_open)
    return 0
