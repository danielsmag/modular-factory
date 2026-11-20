"""The `mfactory` command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .. import checks, config, graph, install
from .. import dashboard as dash
from .. import fleet as fleet_mod
from .. import sync as syncer
from ..engine import Factory
from ..store import Store
from ..util import ConfigError, FactoryError, Refused, find_repo, version
from .fleet import fleet_command
from .maintain import status, status_all, uninstall, upgrade_all, upgrade_self
from .parser import parser


def _stale_note(cfg) -> None:
    """Agents run from the generated files, so say when they no longer match factory.toml."""
    try:
        actions, _notes = syncer.sync(cfg, check=True)
    except ConfigError:
        return
    if actions:
        print(f"note: {len(actions)} generated file(s) are out of date with factory.toml. "
              "Run `.factory/bin/mfactory sync` and commit the result.", flush=True)


def _stdin_text() -> str:
    return "" if sys.stdin.isatty() else sys.stdin.read()


def _inside_repo() -> bool:
    try:
        find_repo()
    except Refused:
        return False
    return True


def _subscribed(repo: Path) -> tuple[fleet_mod.Fleet, str] | None:
    """The fleet and this checkout's label, when the checkout is in the fleet file."""
    try:
        group: fleet_mod.Fleet = fleet_mod.Fleet.find()
    except Refused as error:
        if not str(error).startswith("no fleet"):
            raise
        return None
    resolved: Path = repo.resolve()
    for project in group.projects:
        if project.path == resolved:
            return group, group.label(project)
    return None


def run(args: argparse.Namespace) -> int:
    command: str | None = args.command
    if command == "version":
        print(version())
        return 0
    if command == "fleet":
        return fleet_command(args)
    if command == "hook":
        repo = find_repo(os.environ.get("CLAUDE_PROJECT_DIR"))
        cfg = config.load(repo)
        return checks.hook(cfg, Store(cfg))
    if command == "upgrade" and args.self and args.all:
        raise Refused("pass either --self or --all")
    if command == "upgrade" and args.self:
        upgrade_self(args.ref, args.source)
        return 0
    if command == "upgrade" and args.all:
        return upgrade_all(args.ref, args.source)
    if command == "status" and (args.all or not _inside_repo()):
        return status_all()
    if command == "dashboard" and (args.all or not _inside_repo()):
        group: fleet_mod.Fleet = fleet_mod.Fleet.find()
        fleet_mod.serve(group.file, args.port, not args.no_open)
        return 0

    repo = find_repo()
    if command in ("init", "install"):
        install.init(repo, args.tool)
        return 0
    if command == "upgrade":
        install.upgrade(repo, args.ref, args.source)
        return 0
    if command == "uninstall":
        uninstall(repo, remove_config=args.config, labels=args.labels)
        return 0
    if command == "status":
        return status(repo)
    if command == "doctor":
        return 1 if install.doctor(repo, run_checks=not args.skip_checks) else 0

    cfg = config.load(repo)
    if command == "config":
        print(json.dumps(config.as_json(cfg), indent=1) if args.json else config.show(cfg),
              end="\n" if args.json else "")
    elif command == "graph":
        print(json.dumps(graph.as_json(cfg), indent=1) if args.json else graph.describe(cfg),
              end="\n" if args.json else "")
    elif command == "models":
        print(syncer.describe_models(cfg), end="")
    elif command == "skills":
        print(syncer.describe_skills(cfg), end="")
    elif command == "sync":
        actions, notes = syncer.sync(cfg, check=args.check)
        for action, rel in actions:
            print(f"{'would ' if args.check else ''}{action}  {rel}")
        for note in notes:
            print(f"note: {note}")
        if not actions:
            print(f"up to date: {len(syncer.plan(cfg)[0])} generated files for {', '.join(cfg.tools)}")
        return 1 if args.check and actions else 0
    elif command == "checks":
        return checks.run_here(cfg)
    elif command == "dashboard":
        subscribed: tuple[fleet_mod.Fleet, str] | None = _subscribed(repo)
        if subscribed is None:
            dash.serve(repo, args.port, not args.no_open)
        else:
            group: fleet_mod.Fleet = subscribed[0]
            label: str = subscribed[1]
            fleet_mod.serve(group.file, args.port, not args.no_open, project=label)
    else:
        factory: Factory = Factory(cfg)
        if command == "queue":
            _stale_note(cfg)
            rows = factory.queue()
            for row in rows:
                print("  ".join(str(cell) for cell in row))
            if not rows:
                print(f"nothing to do: no open issue is labeled `{cfg['labels']['queue']}` and no job is unfinished")
        elif command == "create":
            job = factory.create(args.issue)
            print(f"created {job['id']} for #{job['issue']} at {job['step']}")
        elif command == "next":
            factory.next(args.job)
        elif command == "done":
            factory.done(args.job, reply=sys.stdin.read() if args.reply else None)
        elif command == "answer":
            factory.answer(args.job, args.outcome, " ".join(args.text) or _stdin_text())
        elif command == "summary":
            print(factory.summary(args.jobs))
        elif command == "route":
            job = factory.store.read(factory.store.find(args.job))
            print(json.dumps(job["agents"] or factory.route(dict(job)), indent=1))
    return 0


def main(argv: list[str]) -> int:
    arguments: argparse.ArgumentParser = parser()
    args: argparse.Namespace = arguments.parse_args(argv)
    if not args.command:
        arguments.print_help()
        return 0
    try:
        return run(args)
    except ConfigError as error:
        print(f"mfactory: {config.FILE} has {len(error.problems)} problem(s):", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    except FactoryError as error:
        print(f"mfactory: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
