#!/usr/bin/env python3
"""Serve the dashboard with made-up jobs, to look at it while changing it.

    python3 tests/sample_dashboard.py [--port 4747]

It builds a throwaway repository in a temporary folder; nothing here touches
GitHub, Jira or a real project. The project takes jobs from GitHub issues and a
Jira board at once: a stand-in for Jira, from tests/stub_jira.py, serves three
made-up boards.
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core" / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

import stub_jira  # noqa: E402
from modfactory import runstate  # noqa: E402
from modfactory.config import as_json as config_as_json  # noqa: E402
from modfactory.config import load, pipelines_json  # noqa: E402
from modfactory.graph import as_json as graph_as_json  # noqa: E402
from modfactory.store import SCHEMA  # noqa: E402
from modfactory.util import version  # noqa: E402
from modfactory.workspace.jobs import file_rows  # noqa: E402
from modfactory.workspace.registry import Project, Workspace  # noqa: E402
from modfactory.workspace.service import _listen, serve_one  # noqa: E402

SAMPLE_PROJECT_IDS: list[str] = [
    "billing",
    "portal",
    "ops & tools",
    "inventory",
    "payroll",
    "crm",
    "support",
    "analytics",
]

CONFIG = """\
schema = 1

[project]
description = "Invoicing app for small clinics"

[tools]
enabled = ["claude", "cursor"]

[checks]
commands = ["npm ci --prefer-offline", "npm run lint", "npm test -- --run"]

[run]
pipe = "default"

[tracker]
kind = ["github", "jira"]

[tracker.jira]
site = "{site}"
board = 7

[tracker.jira.columns]
queue = "Ready for factory"
intake = "Intake"
ask-author = "Needs info"
spec = "Spec"
spec-approval = "Spec approval"
build = "Build"
gate = "Checks"
debug = "Debug"
review = "Review"
approve = "Approve"
ready-to-merge = "Ready to merge"
verify = "Verify"
escalated = "Escalated"
done = "Done"
rejected = "Rejected"
failed = "Failed"
"""
# The project's pipes: the built-in one with three steps switched on, and a short one for small fixes.
PIPELINES = {
    "default.yaml": """\
# yaml-language-server: $schema=../../.factory/schema/pipe.schema.json
pipes:
  default:
    description: Every change, with a spec a maintainer approves and a smoke test after the merge
    extends: factory:default
    steps:
      spec-approval:
        enabled: true
      debug:
        enabled: true
      gate:
        on:
          FAIL: { max: 3 }
      verify:
        enabled: true
        commands: ["npm run test:smoke"]
""",
    "hotfix.yaml": """\
# yaml-language-server: $schema=../.factory/schema/pipeline.schema.json
# Small fixes: no spec, no approval agent. Still checked and reviewed.
pipes:
  hotfix:
    description: Small fixes, straight from intake to build
    extends: default
    steps:
      spec:
        enabled: false
        skip: DONE
      spec-approval:
        enabled: false
        skip: APPROVE
      approve:
        enabled: false
        skip: APPROVE
      debug:
        enabled: false
      gate:
        on:
          FAIL: { to: build, max: 2 }
""",
}
HOTFIX_SKIPS = {"spec", "spec-approval", "approve"}
# The factory's own board, as a team would lay it out: the factory's steps share one column.
CLINIC_COLUMNS = [
    ("Backlog", ["Backlog"]), ("Ready for factory", ["Ready for factory"]),
    ("In progress", ["Intake", "Spec", "Build", "Checks", "Debug", "Review", "Approve", "Merging", "Verify"]),
    ("Needs a person", ["Needs info", "Spec approval", "Ready to merge", "Ready to verify", "Escalated"]),
    ("Done", ["Done", "Rejected", "Failed"]),
]
TITLES = [
    "Add dark mode to the settings page", "Export invoices as CSV", "Fix rounding of VAT on credit notes",
    "Show a history of reminders sent", "Let admins resend an invoice", "Paginate the patients list",
    "Add a health check endpoint", "Remember the last used filter", "Validate IBAN on the payout form",
    "Speed up the monthly report", "Add keyboard shortcuts to the editor", "Warn before deleting a draft",
    "Support a second logo on invoices", "Translate the reminder emails", "Archive clinics that closed",
    "Show totals per practitioner",
]
HAPPY = [("intake", "ACCEPT", 3), ("spec", "DONE", 6), ("spec-approval", "APPROVE", 95), ("build", "READY", 22),
         ("gate", "PASS", 4), ("review", "PASS", 7), ("approve", "APPROVE", 3), ("merge", "MERGED", 2),
         ("verify", "PASS", 3)]
COST = {"intake": .18, "spec": .42, "build": 2.1, "debug": .5, "review": .9, "approve": .35, "verify": .2}


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def make(jobs_dir, number, title, stop, days_ago, rng, waiting=None, detour=False, pipeline="default"):
    """One job that went along its pipeline's usual path and stopped at `stop`."""
    words = "-".join(title.lower().replace(",", "").split()[:4])
    job_id = f"{number:03d}-{words}"
    t = datetime.now(UTC) - timedelta(days=days_ago, hours=rng.randint(0, 8))
    created, history, cost = t, [], {}
    path = list(HAPPY)
    if detour:
        i = [s for s, _, _ in path].index("gate")
        path[i:i + 1] = [("gate", "FAIL", 4), ("debug", "FOUND", 6), ("build", "READY", 12), ("gate", "PASS", 4)]
        path.insert(i + 5, ("review", "CHANGES", 8))
        path[i + 6:i + 6] = [("build", "READY", 15), ("gate", "PASS", 4)]
    if pipeline == "hotfix":
        path = [row for row in path if row[0] not in HOTFIX_SKIPS]
    step = stop
    for k, (name, outcome, minutes) in enumerate(path):
        if name == stop and stop not in ("done",):
            break
        end = t + timedelta(minutes=minutes * rng.uniform(.6, 1.6))
        nxt = path[k + 1][0] if k + 1 < len(path) else "done"
        skipped = []
        if pipeline == "hotfix":
            skipped = {"intake": ["spec", "spec-approval"], "review": ["approve"]}.get(name, [])
        history.append({"step": name, "outcome": outcome, "entered_at": stamp(t), "at": stamp(end), "reason": None,
                        "to": nxt, "skipped": skipped})
        if name in COST:
            cost[name] = round(cost.get(name, 0) + COST[name] * rng.uniform(.5, 1.5), 2)
        t = end
    if stop in ("rejected", "failed"):
        last = "intake" if stop == "rejected" else "verify"
        history = [h for h in history if h["step"] != last or stop == "failed"]
        history = history[:1] if stop == "rejected" else history
        history[-1].update(outcome="REJECT" if stop == "rejected" else "FAIL", to=stop,
                           reason="Duplicates the export added in #31." if stop == "rejected"
                           else "The commands failed with exit 1.")
    finished = stop in ("done", "rejected", "failed")
    pr = 100 + number if any(h["step"] == "build" for h in history) else None
    job = {
        "id": job_id, "number": number, "issue": 30 + number, "title": title, "pipeline": pipeline,
        "issue_url": f"https://github.com/acme/clinic-invoices/issues/{30 + number}", "author": rng.choice(
            ["mira", "tomas", "ines", "kofi"]),
        "step": step, "entered_at": stamp(t), "running_since": None if finished or waiting else stamp(t),
        "round": 2 if detour else (1 if pr or stop in ("build", "gate") else 0), "attempt": 1, "gate_failed": False,
        "taken": {}, "handoff": [], "branch": f"factory/{job_id}", "worktree": f"/repo/.worktrees/{job_id}",
        "pr": pr, "pr_url": f"https://github.com/acme/clinic-invoices/pull/{pr}" if pr else None,
        "merge_sha": "9f2c1ab" if stop in ("done", "failed") else None,
        "intake": {"decision": "ACCEPT", "risk": rng.choice(["low", "low", "medium", "high"]),
                   "surfaces": rng.choice([[], ["ui"], ["ui"], ["security"]]), "rescoped": False} if history else None,
        "agents": {"reviewers": {"code": "mfactory-reviewer-code"}}, "gate": None, "reviews": {},
        "waiting": {"step": step, "who": waiting, "label": "factory:needs-human", "since": stamp(t),
                    "asked_on": "pr" if pr else "issue"} if waiting else None,
        "consumed": [], "reason": history[-1]["reason"] if finished and history else (
            "[steps.gate].on.FAIL max = 3 reached" if step == "escalated" else None),
        "escalated_from": None, "last_file": None, "verify": None, "history": history,
        "cost_usd": round(sum(cost.values()), 2), "cost_by_step": cost,
        # Plan usage: weekly and 5-hour points per step, and each step's share of this machine's usage.
        "plan_by_step": {step: {"seven_day": round(usd * 0.08, 2), "five_hour": round(usd * 0.6, 2)}
                         for step, usd in cost.items() if step != "intake" or number % 2},
        "usage_share_by_step": {step: round(usd * 0.15, 2) for step, usd in cost.items()},
        "created_at": stamp(created), "finished_at": stamp(t) if finished else None, "updated_at": stamp(t),
    }
    if stop == "done" and number == 1:  # a fix after the merge, from the same job
        url = "https://github.com/acme/clinic-invoices/pull/"
        job["prs"] = [{"number": pr, "url": url + str(pr), "branch": f"factory/{job_id}",
                       "merge_sha": "4be81d0", "round": 1}]
        job.update(pr=pr + 40, pr_url=url + str(pr + 40), branch=f"factory/{job_id}-fix-1", follow_ups=1, round=2)
    folder = jobs_dir / job_id
    (folder / "round-1").mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps(job, indent=1))
    (folder / "issue.md").write_text(f"# Issue #{job['issue']}: {title}\n\n## Body\n\n> Please add this.\n")
    names = {h["step"] for h in history}
    if "intake" in names:
        (folder / "intake.md").write_text(
            f"DECISION: {'REJECT' if stop == 'rejected' else 'ACCEPT'}\nRISK: low\nSURFACES: none\nRESCOPED: no\n\n"
            "## Summary\n- The request is clear and fits the project\n- One small pull request\n")
    if "spec" in names:
        (folder / "spec.md").write_text(
            f"STATUS: DONE\n\n# {title}\n\n## Summary\n"
            "Add the control to the **invoice page**, behind the _existing_ `invoices_v2` flag. "
            "See the [design notes](https://example.com/notes) and keep file_rows unchanged.\n\n"
            "- What will be built\n  - a button in the toolbar\n  - a confirmation dialog\n- What stays the same\n\n"
            "## Acceptance criteria\n"
            "1. `npm test -- --run` passes with the new tests\n2. The page shows the new control\n\n"
            "## Changes\n\n| File | Change |\n|---|---|\n"
            "| `src/invoice.ts` | new handler |\n| `src/toolbar.tsx` | button |\n\n"
            "```ts\nexport function confirmSend(id: string): Promise<boolean> {\n  return dialog.ask(id);\n}\n```\n\n"
            "> Out of scope: the email template.\n")
    if "build" in names:
        (folder / "build.md").write_text("STATUS: READY\n\n## Summary\n- Implemented and tested\n")
    if "gate" in names:
        (folder / "round-1" / "checks.md").write_text("GATE: PASS\nCOMMIT: 9f2c1ab\nATTEMPT: 1\n")
        (folder / "round-1" / "checks.log").write_text("$ npm run lint\n(exit 0)\n")
    if "review" in names:
        (folder / "round-1" / "review-code.md").write_text("VERDICT: PASS\n\n## Findings\nNone.\n")
    if "approve" in names:
        (folder / "decision.md").write_text("DECISION: APPROVE\n\n## Why\nEvery criterion is met.\n")


def sample_queue(now: datetime) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    """Queue rows for the stub CLI, and the issues the stub gh knows. Issue 53 is left out, to show the error."""
    long_label: str = "needs-a-very-long-label-name-that-has-to-wrap-on-a-phone"
    queued: list[tuple[int, str, str, str, list[str], int]] = [
        (51, "new", "Let clinics upload their own logo", "mira", ["factory", "ui"], 3),
        (52, "new", "Send a receipt by text message", "kofi", ["factory"], 9),
        (53, "hold", "Show unpaid invoices first on the dashboard", "ines", ["factory", long_label], 1),
    ]
    rows: list[dict[str, object]] = []
    for number, action, title, author, labels, days_ago in queued:
        opened: datetime = now - timedelta(days=days_ago, hours=2)
        rows.append({
            "action": action, "issue": number, "job": "-", "step": "-",
            "note": "concurrency 3 reached" if action == "hold" else title, "title": title,
            "issue_url": f"https://github.com/acme/clinic-invoices/issues/{number}",
            "created_at": stamp(opened), "updated_at": stamp(opened + timedelta(hours=20)),
            "author": author, "labels": labels,
        })
    body: str = (
        "Clinics want their own logo on invoices and reminders.\n\n"
        "- PNG or SVG, up to 1 MB\n- Shown top left, next to the clinic name\n\n"
        "This text is shown as is: <b>x</b><script>alert(1)</script>\n"
        "Mockups: https://example.com/a/very/long/link/to/the/mockups/that/should/wrap/on/a/narrow/screen/logo.png\n"
    )
    issues: dict[str, dict[str, object]] = {}
    for row in rows[:2]:
        number: object = row["issue"]
        issues[str(number)] = {
            "number": number, "title": row["title"], "state": "OPEN", "url": row["issue_url"],
            "author": {"login": row["author"]}, "labels": [{"name": name} for name in row["labels"]],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "body": body if number == 51 else "", "comments": [],
        }
    issues["51"]["comments"] = [
        {"author": {"login": "tomas"}, "createdAt": stamp(now - timedelta(days=2)),
         "body": "Should the logo also go on the PDF export?"},
        {"author": {"login": "mira"}, "createdAt": stamp(now - timedelta(days=1)),
         "body": "Yes, on every page.\nSame size as the clinic name."},
    ]
    return rows, issues


def stub_github(tmp: Path, issues: dict[str, dict[str, object]]) -> None:
    """Point this server's `gh` at tests/stub_gh.py, with only the sample issues."""
    bin_dir: Path = tmp / "bin"
    bin_dir.mkdir()
    shutil.copy(ROOT / "tests" / "stub_gh.py", bin_dir / "gh")
    (bin_dir / "gh").chmod(0o755)
    state_file: Path = tmp / "gh.json"
    state_file.write_text(json.dumps({
        "user": "operator", "permissions": {}, "labels": ["factory"], "issues": issues, "prs": {},
        "next_number": 100, "next_comment": 0, "calls": [], "fail": None,
    }, indent=1))
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    os.environ["GH_STATE"] = str(state_file)


def sample_jira(now: datetime) -> tuple[stub_jira.FakeJira, str]:
    """A stand-in for Jira with three boards and made-up cards. Returns it and the key of the card a job builds."""
    jira = stub_jira.FakeJira()
    jira.board_name = "Clinic app"
    jira.columns = CLINIC_COLUMNS
    jira.users.update({"ana": "Ana Lima", "kofi": "Kofi Mensah", "lena": "Lena Fischer"})
    today: str = now.strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    recent: str = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
    jira.add_board(8, "Platform", "PLAT", kind="scrum", columns=["To Do", "In Progress", "In Review", "Done"])
    jira.add_board(9, "Support desk", "SUP", columns=["Open", "Waiting on clinic", "Solved"])
    for title, status, who, priority, kind, labels in [
        ("Let patients book appointments online", "Backlog", None, "Medium", "Story", ["booking"]),
        ("Add a clinic switcher to the header", "Ready for factory", "ana", "Medium", "Task", ["ui"]),
        ("Send appointment reminders by SMS", "Build", "op", "High", "Story", ["reminders"]),
    ]:
        jira.ticket(title, status=status, assignee=who, priority=priority, kind=kind, labels=labels, updated=recent)
    building: str = jira.ticket("Show waiting time on the lobby screen", status="Review", assignee="op",
                                priority="Medium", kind="Story", updated=recent)
    jira.ticket("Export invoices to Xero", status="Spec approval", assignee="kofi", priority="High", kind="Story",
                labels=["billing"], updated=today)
    jira.ticket("Fix the time zone of reminder emails", status="Done", assignee="lena", priority="Highest",
                kind="Bug", updated=today)
    for title, status, who, priority, kind, sprint in [
        ("Upgrade Postgres to 17", "In Progress", "lena", "High", "Task", True),
        ("Rotate the payment provider's API keys", "To Do", "kofi", "Highest", "Task", True),
        ("Move CI to bigger runners", "In Review", "ana", "Medium", "Task", True),
        ("Retire the old metrics stack", "To Do", None, "Low", "Task", False),
    ]:
        jira.ticket(title, status=status, assignee=who, priority=priority, kind=kind, project="PLAT", sprint=sprint,
                    updated=recent)
    for title, status, who in [("A clinic cannot log in after the password reset", "Open", "ana"),
                               ("Invoice PDF shows the wrong logo", "Waiting on clinic", "kofi")]:
        jira.ticket(title, status=status, assignee=who, priority="High", kind="Bug", project="SUP", updated=recent)
    return jira, building


def from_jira(jobs_dir: Path, number: int, jira: stub_jira.FakeJira, key: str) -> None:
    """Make sample job `number` one that came from a Jira card."""
    for job_file in jobs_dir.glob(f"{number:03d}-*/job.json"):
        job: dict[str, object] = json.loads(job_file.read_text())
        job.update(issue=key, tracker="jira", issue_url=f"{jira.url}/browse/{key}", author="op",
                   author_name="Olivia Operator")
        job_file.write_text(json.dumps(job, indent=1) + "\n")
        (job_file.parent / "issue.md").write_text(f"# Issue {key}: {job['title']}\n\n## Body\n\n> Please add this.\n")


def more_issues(now: datetime) -> dict[str, dict[str, object]]:
    """Open GitHub issues that are not queued: two the factory is building, and some it is not."""
    found: dict[str, dict[str, object]] = {}
    for number, title, author, labels, assignees, hours_ago in [
        (45, "Archive clinics that closed", "mira", ["factory"], [], 30),
        (47, "Add dark mode to the settings page", "tomas", ["factory"], [], 40),
        (60, "Crash when a patient has no email address", "ines", ["bug"], ["operator"], 5),
        (61, "Docs: how to add a practitioner", "kofi", ["docs"], [], 52),
        (62, "Dark mode for the calendar view", "lena", ["enhancement", "ui"], ["mira"], 75),
    ]:
        updated: datetime = now - timedelta(hours=hours_ago)
        found[str(number)] = {
            "number": number, "title": title, "state": "OPEN",
            "url": f"https://github.com/acme/clinic-invoices/issues/{number}", "author": {"login": author},
            "labels": [{"name": name} for name in labels], "assignees": [{"login": login} for login in assignees],
            "createdAt": stamp(updated - timedelta(days=3)), "updatedAt": stamp(updated), "body": "", "comments": [],
        }
    return found


def publish(repo: Path, jobs_dir: Path, queue: list[dict[str, object]]) -> None:
    """Write the files the dashboard reads, and a stub CLI that prints the sample queue."""
    cfg = load(repo)
    jobs: list[dict[str, object]] = []
    for child in sorted(jobs_dir.iterdir()):
        job_file: Path = child / "job.json"
        if not job_file.is_file():
            continue
        loaded: object = json.loads(job_file.read_text())
        if not isinstance(loaded, dict):
            continue
        loaded["files"] = file_rows(child)
        steps = cfg.pipeline_of(loaded).steps
        loaded.update(status=runstate.job_status(loaded, steps), step_states=runstate.step_states(loaded, steps),
                      edges_taken=runstate.edges_taken(loaded))
        jobs.append(loaded)
    project: object = cfg["project"]
    labels: object = cfg["labels"]
    index: dict[str, object] = {
        "schema": SCHEMA,
        "generated_at": stamp(datetime.now(UTC)),
        "version": version(),
        "project": project if isinstance(project, dict) else {},
        "repo": repo.name,
        "tools": list(cfg.tools),
        "labels": labels if isinstance(labels, dict) else {},
        "tracker": {"kind": "github", "label": "GitHub", "enqueue": "Label an issue `factory`"},
        "trackers": [{"kind": "github", "label": "GitHub", "enqueue": "Label an issue `factory`"},
                     {"kind": "jira", "label": "Jira", "enqueue": "Move a card to `Ready for factory` on board 7"}],
        "jobs": jobs,
    }
    (jobs_dir / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    (jobs_dir / "graph.json").write_text(json.dumps(graph_as_json(cfg), indent=1) + "\n")
    (jobs_dir / "config.json").write_text(json.dumps(config_as_json(cfg), indent=1) + "\n")
    (jobs_dir / "pipelines.json").write_text(json.dumps(pipelines_json(cfg), indent=1) + "\n")
    binary: Path = repo / ".factory" / "bin" / "mfactory"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(queue) + "\nEOF\n")
    binary.chmod(0o755)


def sample_planner(root: Path, label: str, now: datetime) -> None:
    """Planner findings for the badge and the Planner view: one high, one medium."""
    root.mkdir(parents=True, exist_ok=True)

    def ticket(number: int, title: str, priority: str, stage: str) -> dict[str, object]:
        return {"ref": f"{label}:#{number}", "project": label, "key": number, "tracker": "github", "title": title,
                "priority": priority, "url": f"https://github.com/acme/{label}/issues/{number}", "stage": stage,
                "hash": f"h{number}"}

    tickets = [ticket(45, "Export invoices as CSV", "high", "build"),
               ticket(46, "Add a CSV export of invoices", "normal", "review"),
               ticket(47, "Let admins resend an invoice", "critical", "spec-approval"),
               ticket(9, "Validate IBAN on the payment form", "normal", "queued")]
    (root / "snapshot.json").write_text(json.dumps({"schema": 1, "generated_at": stamp(now), "hash": "sample",
                                                    "tickets": tickets, "candidates": [], "errors": []}))
    findings = [
        {"id": "F-1", "key": "duplicate|a", "kind": "duplicate", "tickets": [f"{label}:#45", f"{label}:#46"],
         "severity": "high", "confidence": 0.9, "summary": "Both ask for a CSV export of the invoice list.",
         "evidence": ["similar titles", "same files: app/invoices/export.py"],
         "action": {"type": "close-duplicate", "close": f"{label}:#46", "keep": f"{label}:#45"}, "status": "open"},
        {"id": "F-2", "key": "dependency|b", "kind": "dependency", "tickets": [f"{label}:#9", f"{label}:#47"],
         "severity": "medium", "confidence": 0.7, "summary": "IBAN checks reuse the payment form that #47 changes.",
         "evidence": ["mentions #47"], "action": {"type": "hold", "job": f"{label}:#9", "until": f"{label}:#47"},
         "status": "open"}]
    notes = [{"tickets": [f"{label}:#9", f"{label}:#45"], "text": "not related: different screens"}]
    (root / "findings.json").write_text(json.dumps({"schema": 1, "scanned_at": stamp(now), "snapshot_hash": "sample",
                                                    "frequency": "medium", "alert_at": "high", "next_id": 3,
                                                    "findings": findings, "notes": notes}))


def build_sample_repo(tmp: Path, name: str, empty: bool, rng: random.Random, jira: stub_jira.FakeJira,
                      building: str, queue: list[dict[str, object]]) -> Path:
    repo: Path = tmp / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "factory" / "pipes").mkdir(parents=True)
    (repo / "factory" / "factory.toml").write_text(CONFIG.replace("{site}", jira.url))
    for pipe_name, text in PIPELINES.items():
        (repo / "factory" / "pipes" / pipe_name).write_text(text)
    jobs_dir: Path = repo / ".factory" / "jobs"
    jobs_dir.mkdir(parents=True)
    if not empty:
        plan: list[tuple[object, ...]] = [
            ("done", d, None, d % 3 == 0) for d in (27, 24, 21, 20, 16, 13, 12, 9, 6, 5, 3, 1)
        ]
        plan += [("rejected", 8, None, False), ("failed", 2, None, False), ("build", 0, None, False),
                 ("review", 0, None, True)]
        plan += [("spec-approval", 0, "maintainer", False), ("escalated", 1, "maintainer", True)]
        plan = [row + ("default",) for row in plan]
        plan += [("gate", 0, None, False, "hotfix"), ("done", 4, None, False, "hotfix")]
        for number, (stop, days_ago, waiting, detour, pipe) in enumerate(plan, 1):
            make(jobs_dir, number, TITLES[(number - 1) % len(TITLES)], stop, days_ago, rng, waiting, detour, pipe)
        from_jira(jobs_dir, 16, jira, building)
    publish(repo, jobs_dir, queue)
    return repo


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=4747)
    parser.add_argument("--empty", action="store_true", help="no jobs, to see the empty state")
    parser.add_argument("--projects", type=int, default=1, help="how many sample projects to serve (1–8)")
    args: argparse.Namespace = parser.parse_args()
    if args.projects < 1 or args.projects > 8:
        parser.error("--projects must be between 1 and 8")
    rng: random.Random = random.Random(7)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root: Path = Path(tmp)
        jira: stub_jira.FakeJira
        building: str
        jira, building = sample_jira(datetime.now(UTC))
        os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"] = stub_jira.EMAIL, stub_jira.TOKEN
        os.environ["FACTORY_HOME"] = str(tmp_root / "home")
        (tmp_root / "home").mkdir()
        resets: datetime = datetime.now(UTC) + timedelta(hours=2, minutes=10)
        (tmp_root / "home" / "plan-usage.json").write_text(json.dumps({
            "at": stamp(datetime.now(UTC)),
            "five_hour": {"used_percentage": 37.0, "resets_at": int(resets.timestamp())},
            "seven_day": {"used_percentage": 64.5, "resets_at": int((resets + timedelta(days=3)).timestamp())}}))
        queue: list[dict[str, object]]
        issues: dict[str, dict[str, object]]
        queue, issues = sample_queue(datetime.now(UTC))
        stub_github(tmp_root, issues | more_issues(datetime.now(UTC)))
        seed_name: str = "clinic-invoices"
        label: str = seed_name if args.projects == 1 else SAMPLE_PROJECT_IDS[0]
        sample_planner(tmp_root / "home" / "planner", label, datetime.now(UTC))
        repo: Path = build_sample_repo(tmp_root, seed_name, args.empty, rng, jira, building,
                                       [] if args.empty else queue)
        if args.projects == 1:
            serve_one(repo, args.port, open_browser=False)
            return
        ids: list[str] = SAMPLE_PROJECT_IDS[: args.projects]
        dirs: list[Path] = []
        for project_id in ids:
            dest: Path = tmp_root / project_id
            shutil.copytree(repo, dest)
            dirs.append(dest)
        shutil.rmtree(repo)
        group: Workspace = Workspace(tmp_root / "workspace.toml", [Project(path) for path in dirs])
        _listen(lambda: group, args.port, False, print, None, "mfactory dashboard")


if __name__ == "__main__":
    main()
