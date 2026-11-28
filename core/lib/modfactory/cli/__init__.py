"""The `mfactory` command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from .. import board, checks, config, graph, install, pipelines, plan_usage
from .. import issues as issues_mod
from .. import sync as syncer
from .. import workspace as workspace_mod
from ..engine import Factory
from ..planner import apply as apply_mod
from ..planner import holds as holds_mod
from ..planner import ingest as ingest_mod
from ..planner import schedule as schedule_mod
from ..planner import snapshot as snapshot_mod
from ..planner import summary as summary_mod
from ..planner.state import PlannerState
from ..queue import queue_json
from ..service.tracker import Tracker
from ..store import Store
from ..telemetry.command import run as telemetry_command
from ..util import (
    ConfigError,
    FactoryError,
    Refused,
    find_repo,
    global_binary,
    has_dashboard_page,
    now,
    user_dir,
    version,
)
from ..workspace import planner as workspace_planner
from ..workspace import service as workspace_http
from ..workspace.command import workspace_command
from .maintain import status, status_all, uninstall, upgrade_all, upgrade_everything, upgrade_self
from .parser import parser

DASHBOARD_COMMANDS: frozenset[str] = frozenset({"dashboard", "start", "stop"})


def _problem_lines(factory: Factory) -> None:
    """Each tracker that could not be read while the other was, on stderr, for people and the workspace."""
    for problem in factory.tracker.problems:
        print(f"problem: {problem}", file=sys.stderr)


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
    board_tracker: Tracker | None = factory.tracker.board_member()
    report: dict[str, Any] | None = board_tracker.board_report() if board_tracker else None
    if board_tracker is None or report is None:
        print(f"{factory.tracker.primary.label} issues have no board: {factory.tracker.queue_note()}")
        return 0
    wrong: bool = bool(report["problems"]) or any(row["problem"] for row in report["rows"])
    if as_json:
        print(json.dumps(report))
        return 1 if wrong else 0
    print(f"{board_tracker.label} board {report['board']} at {report['site']}")
    email_from: str = str(report.get("email_from") or "")
    if email_from:
        print(f"account email from {email_from}, token from {report.get('token_from')}")
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
    if not config.config_path(repo).is_file():
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


def _plan_group(repo: Path) -> workspace_mod.Workspace:
    """The workspace this checkout is listed in, or one that holds only this checkout."""
    listed: tuple[workspace_mod.Workspace, str] | None = _listed(repo)
    if listed is None:
        return workspace_mod.Workspace(Path(), [workspace_mod.Project(repo.resolve())])
    return listed[0]


def _plan_collected(repo: Path, cfg: config.Config) -> tuple[dict[str, Any], PlannerState]:
    """Snapshot every project of the workspace. A checkout the workspace does not list is collected on its own."""
    state: PlannerState = PlannerState(user_dir() / "planner")
    return workspace_planner.collect(_plan_group(repo), cfg["planner"], state), state


def _planner_on(cfg: config.Config) -> None:
    if cfg["planner"].get("enabled", True) is False:
        raise Refused("the planner is off: [planner] enabled = false")


def _print_scan_lines(state: PlannerState, then: str) -> None:
    """Freeze the snapshot for the scan agent and print the three lines that start it."""
    # The agent reads a frozen copy: a collect during its scan rewrites snapshot.json.
    state.scan_snapshot_path.write_text(state.snapshot_path.read_text())
    print("start-agent mfactory-planner-scan")
    print(f"prompt: Planner scan. Snapshot: {state.scan_snapshot_path}. Write: {state.draft_path}.")
    print(f"then: {then}")


def plan_scan(repo: Path, cfg: config.Config, force: bool, only_due: bool) -> int:
    """Collect, then say whether the scan agent is due: `scan` / `not due`, or the lines that start it."""
    _planner_on(cfg)
    snapshot, state = _plan_collected(repo, cfg)
    findings: dict[str, Any] = state.read_findings()
    due: bool = force or schedule_mod.is_due(
        str(cfg["planner"]["frequency"]), findings, str(snapshot["hash"]), datetime.now(UTC))
    if only_due:
        print("scan" if due else "not due")
    elif due:
        _print_scan_lines(state, "mfactory plan ingest")
    else:
        print(f"up to date: nothing changed since the scan at {findings.get('scanned_at')}")
    return 0


def plan_summary(repo: Path, cfg: config.Config, fresh: bool, project: str | None, as_json: bool) -> int:
    """The whole picture. With --fresh, a scan the snapshot has outgrown comes first, as the agent's start lines."""
    _planner_on(cfg)
    snapshot, state = _plan_collected(repo, cfg)
    findings: dict[str, Any] = state.read_findings()
    if fresh and (not findings.get("scanned_at") or findings.get("snapshot_hash") != snapshot["hash"]):
        _print_scan_lines(state, "mfactory plan summary")
        return 0
    report: dict[str, Any] = summary_mod.build(snapshot, findings, cfg["planner"], project, datetime.now(UTC))
    print(json.dumps(report, indent=2) if as_json else summary_mod.render(report), end="\n" if as_json else "")
    return 0


def plan_show(cfg: config.Config, as_json: bool) -> int:
    """The findings file, as it is: every finding and its status."""
    _planner_on(cfg)
    state: PlannerState = PlannerState(user_dir() / "planner")
    found: dict[str, Any] = state.read_findings()
    if as_json:
        print(json.dumps({**found, "snapshot_path": str(state.snapshot_path)}, indent=2))
        return 0
    if not found["findings"]:
        print("no findings")
    for f in found["findings"]:
        print(f"{f['id']} {f.get('status')} {f.get('severity')} {f.get('kind')}  {' ↔ '.join(f.get('tickets', []))}"
              f" — {f.get('summary', '')}")
    return 0


def _draft_json(text: str) -> object:
    """The draft a scan agent handed in, tolerating a code fence around it. None when it is not JSON."""
    body: str = text.strip()
    if body.startswith("```"):
        body = body.partition("\n")[2].rpartition("```")[0]
    try:
        return json.loads(body)
    except ValueError:
        return None


def plan_ingest(cfg: config.Config, reply: bool) -> int:
    """Check the draft, merge it into the findings and report what is open."""
    _planner_on(cfg)
    state: PlannerState = PlannerState(user_dir() / "planner")
    snapshot: dict[str, Any] | None = state.read_snapshot(state.scan_snapshot_path) or state.read_snapshot()
    if snapshot is None:
        raise Refused("there is no snapshot: run `mfactory plan scan` first")
    if reply:
        text: str = _stdin_text()
    else:
        try:
            text = state.draft_path.read_text()
        except OSError:
            raise Refused(f"there is no draft at {state.draft_path}") from None
    settings: dict[str, Any] = cfg["planner"]
    draft: object = _draft_json(text)
    if not isinstance(draft, dict) or not isinstance(draft.get("findings"), list):
        raise Refused("the draft is not a JSON object with a findings list; nothing was changed")
    kept, dropped = ingest_mod.validate(draft, snapshot, int(settings["max_extra"]))
    merged: dict[str, Any] = ingest_mod.merge(state.read_findings(), kept, snapshot, settings, now())
    merged["notes"] = ingest_mod.notes(draft, snapshot)
    state.write_findings(merged)
    state.draft_path.unlink(missing_ok=True)
    opened: list[dict[str, Any]] = [f for f in merged["findings"] if f.get("status") == "open"]
    high: int = sum(1 for f in opened if f.get("severity") == "high")
    print(f"{len(opened)} open findings ({high} high)")
    for reason in dropped:
        print(f"dropped: {reason}")
    return 0


def plan_suggest(cfg: config.Config) -> int:
    """Add the one entry of a Draft on standard input as an open finding. The other findings stay as they are."""
    _planner_on(cfg)
    state: PlannerState = PlannerState(user_dir() / "planner")
    snapshot: dict[str, Any] | None = state.read_snapshot(state.scan_snapshot_path) or state.read_snapshot()
    if snapshot is None:
        raise Refused("there is no snapshot: run `mfactory plan scan` first")
    draft: object = _draft_json(_stdin_text())
    if not isinstance(draft, dict) or not isinstance(draft.get("findings"), list) or len(draft["findings"]) != 1:
        raise Refused('the suggestion is not a JSON object with a findings list of one entry: {"findings": [...]}')
    settings: dict[str, Any] = cfg["planner"]
    kept, dropped = ingest_mod.validate(draft, snapshot, int(settings["max_extra"]))
    for reason in dropped:
        print(f"dropped: {reason}")
    if not kept:
        return 1
    old: dict[str, Any] = state.read_findings()
    merged: dict[str, Any] = ingest_mod.merge(old, kept, snapshot, settings, now())
    # A suggestion is not a scan: merge drops the open findings the entry does not repeat, and stamps a scan.
    here: set[str] = {str(f.get("id")) for f in merged["findings"]}
    merged["findings"] += [f for f in old["findings"] if f.get("status") == "open" and f.get("id") not in here]
    merged["findings"].sort(key=lambda f: int(str(f.get("id")).partition("-")[2] or 0))
    merged.update(scanned_at=old.get("scanned_at"), snapshot_hash=old.get("snapshot_hash"), notes=old.get("notes", []))
    key: str = ingest_mod.finding_key(kept[0]["kind"], kept[0]["tickets"])
    added: dict[str, Any] = next(f for f in merged["findings"] if f.get("key") == key)
    if added.get("status") != "open":
        print(f"not added: {added['id']} is {added.get('status')} and its tickets have not changed")
        return 1
    state.write_findings(merged)
    print(f"{added['id']} open")
    return 0


def _issue_command(factory: Factory, args: argparse.Namespace) -> int:
    """`mfactory issue ...`: a person's own issues; see `issues`."""
    what: str = args.issue_what
    if what == "list":
        return issues_mod.list_issues(factory, args.tracker, args.mine, args.json)
    if what == "show":
        return issues_mod.show_issue(factory, args.issue, args.json)
    if what == "create":
        return issues_mod.create_issue(factory, args.tracker, " ".join(args.title), _stdin_text(), args.queue)
    if what == "comment":
        return issues_mod.comment_issue(factory, args.issue, " ".join(args.text) or _stdin_text())
    if what == "move":
        return issues_mod.move_issue(factory, args.issue, " ".join(args.status))
    return issues_mod.close_issue(factory, args.issue, args.not_planned)


def plan_release(factory: Factory, ref: str) -> int:
    """Lift the hold of a job or a queued issue. `ref` is a job id or an issue key."""
    key: Any
    try:
        key = factory.store.read(factory.store.find(ref))["issue"]
    except Refused:
        key = factory.tracker.key(ref)
    if holds_mod.release(factory, key):
        print(f"released {ref}")
        return 0
    print(f"not held: {ref}")
    return 1


def _open_finding(state: PlannerState, name: str) -> dict[str, Any]:
    """The finding called `name`, which must still be open."""
    for finding in state.read_findings()["findings"]:
        if finding.get("id") == name:
            if finding.get("status") != "open":
                raise Refused(f"{name} is already {finding.get('status')}")
            return finding
    raise Refused(f"no finding {name}")


def _decide(state: PlannerState, name: str, status: str, result: str) -> None:
    """Record the decision on `name`, from a fresh read of the findings file."""
    findings: dict[str, Any] = state.read_findings()
    for finding in findings["findings"]:
        if finding.get("id") == name:
            finding.update(status=status, decided_at=now(), result=result)
    state.write_findings(findings)


def plan_approve(repo: Path, cfg: config.Config, name: str) -> int:
    """Run an open finding's action in the project that owns its first ticket, then mark the finding approved."""
    _planner_on(cfg)
    state: PlannerState = PlannerState(user_dir() / "planner")
    finding: dict[str, Any] = _open_finding(state, name)
    snapshot: dict[str, Any] = state.read_snapshot() or {}
    by_ref: dict[str, dict[str, Any]] = {t["ref"]: t for t in snapshot.get("tickets", []) if isinstance(t, dict)}
    tickets: list[str] = [str(ref) for ref in finding.get("tickets", [])]
    action: dict[str, Any] = apply_mod.check(finding.get("action"), set(tickets))
    group: workspace_mod.Workspace = _plan_group(repo)
    refs: dict[str, dict[str, Any]] = {}
    for ref in tickets:
        if ref not in by_ref:
            raise Refused(f"{ref} is not in the snapshot any more: run `mfactory plan collect`")
        project: workspace_mod.Project | None = group.project(str(by_ref[ref].get("project")))
        if project is None:
            raise Refused(f"{ref}: the workspace has no project {by_ref[ref].get('project')}")
        refs[ref] = {"project": str(project.path), "key": by_ref[ref]["key"], "url": by_ref[ref].get("url") or ""}
    print(f"will: {apply_mod.describe(action)}")
    owner: workspace_mod.Project | None = group.project(str(by_ref[apply_mod.first_target(action)]["project"]))
    assert owner is not None  # every ticket of the finding resolved above
    worked, said = workspace_planner.apply_in(owner, action, refs)
    if not worked:
        print(f"mfactory: {said}", file=sys.stderr)
        return 1
    _decide(state, name, "approved", said)
    print(said)
    return 0


def plan_dismiss(cfg: config.Config, name: str, reason: list[str]) -> int:
    """Mark an open finding dismissed: the scan keeps it dismissed until one of its tickets changes."""
    _planner_on(cfg)
    state: PlannerState = PlannerState(user_dir() / "planner")
    _open_finding(state, name)
    _decide(state, name, "dismissed", " ".join(reason) or "dismissed")
    print(f"dismissed {name}")
    return 0


def plan_apply(factory: Factory, action_json: str, refs_json: str) -> int:
    """Carry out one action. `refs_json` maps each ref the action may name to {project, key, url}."""
    try:
        action: object = json.loads(action_json)
        given: object = json.loads(refs_json)
    except ValueError:
        raise Refused("--action and --refs must be JSON") from None
    if not isinstance(given, dict) or not all(
            isinstance(v, dict) and isinstance(v.get("project"), str) and isinstance(v.get("key"), int | str)
            for v in given.values()):
        raise Refused('--refs must be an object of {"<ref>": {"project": <path>, "key": <key>, "url": <url>}}')
    known: dict[str, tuple[Path, Any]] = {str(ref): (Path(v["project"]), v["key"]) for ref, v in given.items()}
    urls: dict[str, str] = {str(ref): str(v.get("url") or "") for ref, v in given.items()}
    print(apply_mod.apply_action(factory, action, known, urls))
    return 0


def plan_collect(repo: Path, cfg: config.Config, as_json: bool) -> int:
    """Snapshot every project of the workspace and print what was found."""
    found: dict[str, Any] = _plan_collected(repo, cfg)[0]
    if as_json:
        print(json.dumps(found, indent=2))
        return 0
    count: int = len(found["candidates"])
    print(f"{len(found['tickets'])} tickets, {count} pair{'' if count == 1 else 's'}")
    for ref in found["released"]:
        print(f"released {ref}")
    for line in found["errors"]:
        print(f"problem: {line}")
    return 0


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
        raise Refused(
            "the dashboard comes with the global install. Run `uv tool install modular-factory`."
        )
    if binary.resolve() == Path(sys.argv[0]).resolve():
        raise Refused(f"{binary.parents[2] / 'web'} is missing. Run `mfactory upgrade --self`.")
    os.execv(sys.executable, [sys.executable, str(binary), *argv])


def pipelines_command(cfg: config.Config, args: argparse.Namespace) -> int:
    """`mfactory pipes list|show|validate|schema`."""
    what: str = args.pipes_what
    if what == "schema":
        print(pipelines.SCHEMA_FILE.read_text(), end="")
        return 0
    if what == "show":
        shown: config.Config = cfg.view(args.name)
        pipe: pipelines.Pipeline = shown.pipeline
        print(pipelines.dump({pipelines.KEY: {pipe.name: {
            "description": pipe.description, "start": pipe.start, "escalate_to": pipe.escalate_to,
            "steps": {name: _flat_edges(step) for name, step in pipe.steps.items()}}}}), end="")
        return 0
    if what == "validate":
        # config.load already refused any problem; reaching here means every file is valid.
        for report in cfg.pipeline_files:
            print(f"ok    {report.path}: {', '.join(report.names) or 'no pipes'}")
        if not cfg.pipeline_files:
            print("ok    no pipe files: jobs run the built-in default pipe")
        print(f"{len(cfg.pipelines)} pipe(s) valid: {', '.join(cfg.pipelines)}")
        return 0
    chosen: str = str(cfg["run"].get("pipe") or "")
    ask: bool = not chosen and len(cfg.pipelines) > 1
    if args.json:
        print(json.dumps({
            "default": chosen or (next(iter(cfg.pipelines)) if len(cfg.pipelines) == 1 else None),
            "ask": ask,
            "pipes": [{"name": name, "description": pipe.description, "file": pipe.file,
                           "extends": pipe.extends, "start": pipe.start,
                           "steps": [n for n, s in pipe.steps.items() if graph.enabled(s)]}
                          for name, pipe in cfg.pipelines.items()],
        }, indent=1))
        return 0
    width: int = max(len(name) for name in cfg.pipelines)
    for name, pipe in cfg.pipelines.items():
        mark: str = "*" if name == chosen else " "
        on: int = sum(1 for s in pipe.steps.values() if graph.enabled(s))
        print(f"{mark} {name.ljust(width)}  {pipe.file:<28} {on:>2} steps  {pipe.description}")
    print("")
    print(f"[run].pipe = {json.dumps(chosen)}" + (
        ": `/mfactory run` asks which pipe each new job runs" if ask else ""))
    return 0


def _flat_edges(step: dict[str, Any]) -> dict[str, Any]:
    """A step with each `{ to }` edge written as the bare step name, as people write it."""
    if not isinstance(step.get("on"), dict):
        return step
    edges: dict[str, Any] = {k: v["to"] if isinstance(v, dict) and set(v) == {"to"} else v
                             for k, v in step["on"].items()}
    return dict(step, on=edges)


def _move(repo: Path, source: Path, target: Path) -> None:
    """git mv when git tracks `source`, so its history follows; a plain move otherwise."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tracked: bool = subprocess.run(["git", "ls-files", "--error-unmatch", str(source)], cwd=repo,
                                   capture_output=True).returncode == 0
    if tracked and subprocess.run(["git", "mv", str(source), str(target)], cwd=repo,
                                  capture_output=True).returncode == 0:
        return
    shutil.move(str(source), str(target))


def migrate_layout(repo: Path) -> int:
    """`mfactory migrate layout`: factory.toml at the root moves to factory/factory.toml, with the pipes beside it."""
    old: Path = repo / config.FILE
    new: Path = repo / config.DIR / config.FILE
    moved: list[str] = []
    if old.is_file():
        if new.exists():
            raise Refused(f"both {config.FILE} and {config.DIR}/{config.FILE} exist. Keep one, then run this again.")
        _move(repo, old, new)
        moved.append(f"{config.FILE} -> {config.DIR}/{config.FILE}")
    # Pipes from before they moved into factory/: a pipelines/ folder at the root, its files under `pipelines:`.
    early: Path = repo / "pipelines"
    pipes_dir: Path = repo / pipelines.FOLDER
    if pipelines.old_folder_in_use(repo):
        for path in sorted(p for p in early.iterdir() if p.suffix in pipelines.SUFFIXES):
            target: Path = pipes_dir / path.name
            _move(repo, path, target)
            text: str = re.sub(r"(?m)^pipelines:", f"{pipelines.KEY}:", target.read_text())
            target.write_text(text.replace("../.factory/schema/pipeline.schema.json",
                                           "../../.factory/schema/pipe.schema.json"))
            moved.append(f"pipelines/{path.name} -> {pipelines.FOLDER}/{path.name}")
        if early.is_dir() and not any(early.iterdir()):
            early.rmdir()
    moved += _rename_old_keys(config.config_path(repo))
    if not moved:
        print(f"nothing to move: {config.config_rel(repo)} is in place")
        return 0
    config.load(repo)  # the moved files must still load where they now are
    for line in moved:
        print(f"moved {line}")
    print(f"Commit {config.DIR}/ and the removed files.")
    return 0


def _rename_old_keys(path: Path) -> list[str]:
    """[run].pipeline becomes [run].pipe; [paths].pipelines is dropped when it named pipelines/, else renamed."""
    if not path.is_file():
        return []
    table: str = ""
    lines: list[str] = []
    done: list[str] = []
    for line in path.read_text().splitlines():
        header: str = line.strip()
        if header.startswith("[") and header.endswith("]") and not header.startswith("[["):
            table = header.strip("[]").strip()
        key: re.Match[str] | None = re.match(r"(\s*)(pipeline|pipelines)(\s*=\s*)(.*)$", line)
        if key and table == "run" and key.group(2) == "pipeline":
            line = f"{key.group(1)}pipe{key.group(3)}{key.group(4)}"
            done.append("[run].pipeline -> [run].pipe")
        elif key and table == "paths" and key.group(2) == "pipelines":
            done.append("[paths].pipelines -> [paths].pipes" if not re.match(r'"pipelines/?"', key.group(4))
                        else "[paths].pipelines removed: the pipes are in factory/pipes/")
            if re.match(r'"pipelines/?"', key.group(4)):
                continue
            line = f"{key.group(1)}pipes{key.group(3)}{key.group(4)}"
        lines.append(line)
    if done:
        path.write_text("\n".join(lines) + "\n")
    return done


def migrate_pipelines(repo: Path) -> int:
    """`mfactory migrate pipes`: [steps.*] and [run].start/escalate_to in factory.toml move to YAML."""
    path: Path = config.config_path(repo)
    text: str = path.read_text()
    data: dict[str, Any] = config.read_toml(path, config.config_rel(repo))
    run_table: dict[str, Any] = data.get("run") or {}
    legacy: dict[str, Any] = {k: run_table[k] for k in ("start", "escalate_to") if k in run_table}
    steps: dict[str, Any] = data.get("steps") or {}
    if not steps and not legacy:
        print(f"nothing to migrate: {config.FILE} has no [steps.*] and no [run].start or escalate_to")
        return 0
    cfg: config.Config = config.load(repo)
    pipe: pipelines.Pipeline = cfg.pipelines[pipelines.DEFAULT]
    folder: Path = config.pipes_folder(cfg)
    target: Path = folder / "default.yaml"
    if pipe.file not in ("built-in", target.relative_to(repo).as_posix()):
        raise Refused(f"the default pipeline is declared in {pipe.file}. Move [steps.*] there by hand.")
    folder.mkdir(parents=True, exist_ok=True)
    body: str = pipelines.dump({pipelines.KEY: {pipelines.DEFAULT: {
        "description": pipe.description, "start": pipe.start, "escalate_to": pipe.escalate_to,
        "steps": {name: _flat_edges(step) for name, step in pipe.steps.items()}}}})
    head: str = pipelines.starter_text().split(f"\n{pipelines.KEY}:\n", 1)[0]
    target.write_text(head + "\n" + body)
    kept: list[str] = []
    skipping: bool = False
    for line in text.splitlines():
        header: str = line.strip()
        if header.startswith("[") and not header.startswith("[["):
            skipping = header.startswith("[steps.") or header == "[steps]"
        if skipping:
            continue
        if re.match(r"\s*(start|escalate_to)\s*=", line) and _in_table(text, line, "run"):
            continue
        kept.append(line)
    path.write_text("\n".join(kept).rstrip("\n") + "\n")
    config.load(repo)  # the moved settings must still be valid where they now live
    print(f"wrote {target.relative_to(repo).as_posix()} and removed [steps.*] "
          f"and [run].start/escalate_to from {config.config_rel(repo)}")
    return 0


def _in_table(text: str, wanted_line: str, table: str) -> bool:
    current: str = ""
    for line in text.splitlines():
        header: str = line.strip()
        if header.startswith("[") and header.endswith("]"):
            current = header.strip("[]").strip()
        if line == wanted_line:
            return current == table
    return False


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

    if command == "migrate":
        return migrate_layout(repo) if args.what == "layout" else migrate_pipelines(repo)
    cfg = config.load(repo)
    if command == "telemetry":
        return telemetry_command(cfg, args)
    if command == "config":
        print(json.dumps(config.as_json(cfg), indent=1) if args.json else config.show(cfg),
              end="\n" if args.json else "")
    elif command == "graph":
        shown: config.Config = cfg.view(args.pipe)
        print(json.dumps(graph.as_json(shown), indent=1) if args.json else graph.describe(shown),
              end="\n" if args.json else "")
    elif command in ("pipes", "pipelines"):
        return pipelines_command(cfg, args)
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
    elif command == "plan" and args.plan_what == "collect":
        return plan_collect(repo, cfg, args.json)
    elif command == "plan" and args.plan_what in ("scan", "due"):
        return plan_scan(repo, cfg, args.plan_what == "scan" and args.force, args.plan_what == "due")
    elif command == "plan" and args.plan_what == "summary":
        return plan_summary(repo, cfg, args.fresh, args.project, args.json)
    elif command == "plan" and args.plan_what == "show":
        return plan_show(cfg, args.json)
    elif command == "plan" and args.plan_what == "suggest":
        return plan_suggest(cfg)
    elif command == "plan" and args.plan_what == "ingest":
        return plan_ingest(cfg, args.reply)
    elif command == "plan" and args.plan_what == "approve":
        return plan_approve(repo, cfg, args.finding)
    elif command == "plan" and args.plan_what == "dismiss":
        return plan_dismiss(cfg, args.finding, args.reason)
    elif command == "issue" and args.issue_what == "guide":
        return issues_mod.guide(cfg, args.json)
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
            rows, issues = factory.queue_and_issues()
            watch_rows, watched = factory.watch_rows()
            print(json.dumps(queue_json(rows + watch_rows, issues + watched)))
            _problem_lines(factory)
        elif command == "queue":
            _stale_note(cfg)
            _index_note(factory, sys.stdout)
            rows = factory.queue()
            for row in rows:
                print("  ".join(str(cell) for cell in row))
            if not rows:
                print(f"nothing to do: {factory.tracker.empty_note()} and no job is unfinished")
            _problem_lines(factory)
        elif command == "plan" and args.plan_what == "snapshot":
            name: str = args.label or cfg.repo.name
            found, problems = snapshot_mod.project_tickets(factory, name)
            print(json.dumps({"label": name, "tickets": found, "errors": problems}, indent=None if args.json else 2))
        elif command == "plan" and args.plan_what == "release":
            return plan_release(factory, args.ref)
        elif command == "plan" and args.plan_what == "apply":
            return plan_apply(factory, args.action, args.refs)
        elif command == "create":
            job = factory.create(args.issue, args.pipe)
            chose: str = f" (pipe {job['pipeline']})" if len(cfg.pipelines) > 1 else ""
            print(f"created {job['id']} for {factory.tracker.ref(job['issue'])} at {job['step']}{chose}")
        elif command == "file":
            text: str = _stdin_text()
            if not text.strip():
                raise Refused("the issue's text goes on stdin")
            target: Tracker = factory.tracker.member(args.tracker) if args.tracker else factory.tracker.primary
            print(target.create_issue(" ".join(args.title), text, queue=True))
        elif command == "issue":
            return _issue_command(factory, args)
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
            if factory.tracker.board_member() is None:
                print(f"{factory.tracker.primary.label} issues have no board: nothing to move")
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
        yaml_too: bool = any(".yaml" in problem or ".yml" in problem for problem in error.problems)
        where: str = "the configuration" if yaml_too else config.FILE
        print(f"mfactory: {where} has {len(error.problems)} problem(s):", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    except FactoryError as error:
        print(f"mfactory: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
