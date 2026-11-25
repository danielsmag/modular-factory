"""The `mfactory` command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from .. import board, checks, config, graph, install, plan_usage
from .. import sync as syncer
from .. import workspace as workspace_mod
from ..engine import Factory
from ..queue import queue_json
from ..store import Store
from ..util import ConfigError, FactoryError, Refused, find_repo, global_binary, has_dashboard_page, version
from ..workspace import service as workspace_http
from ..workspace.command import workspace_command
from .maintain import status, status_all, uninstall, upgrade_all, upgrade_everything, upgrade_self
from .parser import parser

DASHBOARD_COMMANDS: frozenset[str] = frozenset({"dashboard", "start", "stop"})


def _stale_note(cfg, out: TextIO = sys.stdout) -> None:
    """Agents run from the generated files, so say when they no longer match factory.toml."""
    try:
        actions, _notes = syncer.sync(cfg, check=True)
    except ConfigError:
        return
    if actions:
        print(f"note: {len(actions)} generated file(s) are out of date with factory.toml. "
              "Run `.factory/bin/mfactory sync` and commit the result.", file=out, flush=True)


def _index_note(factory: Factory, out: TextIO) -> None:
    """Say when the dashboard index listed job folders that are gone."""
    dropped: list[str] = factory.store.reconcile_index()
    if not dropped:
        return
    print(f"note: index.json dropped {', '.join(dropped)} (job folder gone)", file=out, flush=True)


def _tracker_suggest(cfg: Any, as_json: bool) -> None:
    """`mfactory tracker suggest`: the board for this pipeline. Read from factory.toml only; Jira is not asked."""
    found: dict[str, Any] = board.suggest(cfg.steps, cfg["run"]["start"], cfg["run"]["escalate_to"])
    if as_json:
        print(json.dumps(found))
        return
    print("A Jira board for this pipeline, left to right. Name each column's status the same.")
    print()
    notes: dict[str, str] = {board.BACKLOG: f"not the factory's: move a card to `{board.READY}` to build it",
                             board.READY: "the queue: the top card is built first"}
    rows: list[tuple[str, str, str]] = [("column", "who", "steps")]
    rows += [(c["name"], c["who"], notes.get(c["name"]) or ", ".join(c["steps"])) for c in found["columns"]]
    widths: list[int] = [max(len(row[i]) for row in rows) for i in (0, 1)]
    for name, who, steps in rows:
        print(f"  {name.ljust(widths[0])}  {who.ljust(widths[1])}  {steps}")
    print()
    print(f"Put {', '.join(f'`{n}`' for n in found['done_category'])} in Jira's Done category, and every other "
          "status in To Do or In Progress.")
    print("Let every status move to every other in the workflow. Steps that are switched off get no column. "
          "Run this again after turning a step on.")
    for note in found["notes"]:
        print(f"note: {note}")
    print()
    print("Paste this into factory.toml, then run `mfactory tracker show`:")
    print()
    print(found["toml"], end="")


def _tracker_show(factory: Factory, as_json: bool) -> int:
    """`mfactory tracker show`: each step, its status and its board column. 1 when the board has a problem."""
    report: dict[str, Any] | None = factory.tracker.board_report()
    if report is None:
        print(f"{factory.tracker.label} issues have no board: {factory.tracker.queue_note()}")
        return 0
    wrong: bool = bool(report["problems"]) or any(row["problem"] for row in report["rows"])
    if as_json:
        print(json.dumps(report))
        return 1 if wrong else 0
    print(f"{factory.tracker.label} board {report['board']} at {report['site']}")
    table: list[tuple[str, str, str, str, str]] = [("step", "type", "status", "column", "")]
    for row in report["rows"]:
        note: str = row["problem"] or ("switched off" if not row["enabled"] else
                                       "" if row["status"] else "no status: the card stays where it is")
        table.append((row["step"], row["type"], row["status"] or "-", row["column"] or "-", note))
    widths: list[int] = [max(len(line[i]) for line in table) for i in range(4)]
    for line in table:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=False)) + (
            f"  {line[4]}" if line[4] else ""))
    if report["unused"]:
        print(f"statuses no step uses: {', '.join(report['unused'])}")
    for problem in report["problems"]:
        print(f"problem: {problem}")
    for warning in report.get("warnings") or []:
        print(f"warning: {warning}")
    if report["unchecked"]:
        print(f"transitions not checked from: {', '.join(report['unchecked'])} (no card is in those statuses now)")
    if report.get("note"):
        print(report["note"])
    return 1 if wrong else 0


def _stdin_text() -> str:
    return "" if sys.stdin.isatty() else sys.stdin.read()


def _inside_repo() -> bool:
    try:
        find_repo()
    except Refused:
        return False
    return True


def _project_repo() -> Path | None:
    """A checkout with factory.toml. A plain git repository is not a project."""
    try:
        repo: Path = find_repo()
    except Refused:
        return None
    if not (repo / config.FILE).is_file():
        return None
    return repo


def _listed(repo: Path) -> tuple[workspace_mod.Workspace, str] | None:
    """The workspace and this checkout's label, when the checkout is in the workspace file."""
    try:
        group: workspace_mod.Workspace = workspace_mod.Workspace.find()
    except Refused as error:
        if not str(error).startswith("no workspace"):
            raise
        return None
    resolved: Path = repo.resolve()
    for project in group.projects:
        if project.path == resolved:
            return group, group.label(project)
    return None


def _add_to_workspace(repo: Path) -> None:
    """List a fresh install in the user's workspace, so the dashboard shows it."""
    if _listed(repo) is not None:
        return
    group: workspace_mod.Workspace = workspace_mod.Workspace.locate(create=True)
    group.add(repo)
    print(f"ok    added to {group.file}")


def _hand_off(argv: list[str]) -> None:
    """Run a dashboard command with the global `mfactory`, which has the page. Does not return."""
    binary: Path | None = global_binary()
    if binary is None:
        raise Refused("the dashboard comes with the global install. Run install.sh --global.")
    if binary.resolve() == Path(sys.argv[0]).resolve():
        raise Refused(f"{binary.parents[2] / 'web'} is missing. Run `mfactory upgrade --self`.")
    os.execv(sys.executable, [sys.executable, str(binary), *argv])


def run(args: argparse.Namespace) -> int:
    command: str | None = args.command
    if command == "version":
        print(version())
        return 0
    if command == "start":
        return workspace_http.start_dashboard(args)
    if command == "stop":
        return workspace_http.stop_dashboard()
    if command == "workspace":
        return workspace_command(args)
    if command == "statusline":
        return plan_usage.main(args.then)
    if command == "hook":
        repo = find_repo(os.environ.get("CLAUDE_PROJECT_DIR"))
        cfg = config.load(repo)
        return checks.hook(cfg, Store(cfg))
    if command == "upgrade" and args.self and not args.all:
        return upgrade_self(args.ref, args.source, dry_run=args.dry_run)
    if command == "upgrade" and args.all and not args.self:
        return upgrade_all(args.ref, args.source, dry_run=args.dry_run)
    if command == "upgrade":
        return upgrade_everything(args.ref, args.source, dry_run=args.dry_run)
    if command == "status" and (args.all or not _inside_repo()):
        return status_all()
    if command == "dashboard" and _project_repo() is None:
        group: workspace_mod.Workspace = workspace_mod.Workspace.find()
        workspace_http.serve(group.file, args.port, not args.no_open)
        return 0

    repo = find_repo()
    if command in ("init", "install"):
        install.init(repo, args.tool, personal=args.personal)
        _add_to_workspace(repo)
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
        listed: tuple[workspace_mod.Workspace, str] | None = _listed(repo)
        if listed is None:
            workspace_http.serve_one(repo, args.port, not args.no_open)
        else:
            group: workspace_mod.Workspace = listed[0]
            label: str = listed[1]
            workspace_http.serve(group.file, args.port, not args.no_open, project=label)
    else:
        factory: Factory = Factory(cfg)
        if command == "queue" and args.json:
            _stale_note(cfg, sys.stderr)
            _index_note(factory, sys.stderr)
            queued: tuple[list[tuple[Any, ...]], list[dict[str, Any]]] = factory.queue_and_issues()
            print(json.dumps(queue_json(*queued)))
        elif command == "queue":
            _stale_note(cfg)
            _index_note(factory, sys.stdout)
            rows = factory.queue()
            for row in rows:
                print("  ".join(str(cell) for cell in row))
            if not rows:
                print(f"nothing to do: {factory.tracker.empty_note()} and no job is unfinished")
        elif command == "create":
            job = factory.create(args.issue)
            print(f"created {job['id']} for {factory.tracker.ref(job['issue'])} at {job['step']}")
        elif command == "file":
            text: str = _stdin_text()
            if not text.strip():
                raise Refused("the issue's text goes on stdin")
            print(factory.tracker.create_issue(" ".join(args.title), text, queue=True))
        elif command == "next":
            factory.next(args.job)
        elif command == "done":
            factory.done(args.job, reply=sys.stdin.read() if args.reply else None)
        elif command == "answer":
            factory.answer(args.job, args.outcome, " ".join(args.text) or _stdin_text())
        elif command == "close" and args.close_what == "issue":
            factory.close_issue(args.job, not_planned=args.not_planned)
        elif command == "close":
            factory.close_job(args.job, merged=args.merged)
        elif command == "delete":
            factory.delete(args.job, worktree=args.worktree, branch=args.branch, remote=args.remote,
                           pr=args.pr, force=args.force)
        elif command == "move":
            factory.move(args.job, args.step, " ".join(args.text) or _stdin_text())
        elif command == "route":
            job = factory.store.read(factory.store.find(args.job))
            print(json.dumps(job["agents"] or factory.route(dict(job)), indent=1))
        elif command == "tracker" and args.tracker_what == "suggest":
            _tracker_suggest(cfg, args.json)
        elif command == "tracker" and args.tracker_what == "show":
            return _tracker_show(factory, args.json)
        elif command == "tracker":
            if factory.tracker.board_report() is None:
                print(f"{factory.tracker.label} issues have no board: nothing to move")
                return 0
            return 0 if factory.sync_cards() else 1
    return 0


def main(argv: list[str]) -> int:
    arguments: argparse.ArgumentParser = parser()
    args: argparse.Namespace = arguments.parse_args(argv)
    if not args.command:
        arguments.print_help()
        return 0
    try:
        if args.command in DASHBOARD_COMMANDS and not has_dashboard_page():
            _hand_off(argv)
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
