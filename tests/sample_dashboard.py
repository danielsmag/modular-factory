#!/usr/bin/env python3
"""Serve the dashboard with made-up jobs, to look at it while changing it.

    python3 tests/sample_dashboard.py [--port 4747]

It builds a throwaway repository in a temporary folder; nothing here touches
GitHub or a real project.
"""

import argparse
import json
import random
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core" / "lib"))

from modfactory.config import as_json as config_as_json  # noqa: E402
from modfactory.config import load  # noqa: E402
from modfactory.graph import as_json as graph_as_json  # noqa: E402
from modfactory.store import SCHEMA  # noqa: E402
from modfactory.util import version  # noqa: E402
from modfactory.workspace.jobs import file_rows  # noqa: E402
from modfactory.workspace.service import serve_one  # noqa: E402

CONFIG = """\
schema = 1

[project]
description = "Invoicing app for small clinics"

[tools]
enabled = ["claude", "cursor"]

[checks]
commands = ["npm ci --prefer-offline", "npm run lint", "npm test -- --run"]

[steps.spec-approval]
enabled = true

[steps.debug]
enabled = true

[steps.verify]
enabled = true
commands = ["npm run test:smoke"]

[steps.gate]
on = { FAIL = { max = 3 } }
"""
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


def make(jobs_dir, number, title, stop, days_ago, rng, waiting=None, detour=False):
    """One job that went along the default path and stopped at `stop`."""
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
    step = stop
    for k, (name, outcome, minutes) in enumerate(path):
        if name == stop and stop not in ("done",):
            break
        end = t + timedelta(minutes=minutes * rng.uniform(.6, 1.6))
        nxt = path[k + 1][0] if k + 1 < len(path) else "done"
        history.append({"step": name, "outcome": outcome, "entered_at": stamp(t), "at": stamp(end), "reason": None,
                        "to": nxt, "skipped": []})
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
        "id": job_id, "number": number, "issue": 30 + number, "title": title,
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
        "created_at": stamp(created), "finished_at": stamp(t) if finished else None, "updated_at": stamp(t),
    }
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
            f"STATUS: DONE\n\n# {title}\n\n## Summary\n- What will be built\n\n## Acceptance criteria\n"
            "1. `npm test -- --run` passes with the new tests\n2. The page shows the new control\n")
    if "build" in names:
        (folder / "build.md").write_text("STATUS: READY\n\n## Summary\n- Implemented and tested\n")
    if "gate" in names:
        (folder / "round-1" / "checks.md").write_text("GATE: PASS\nCOMMIT: 9f2c1ab\nATTEMPT: 1\n")
        (folder / "round-1" / "checks.log").write_text("$ npm run lint\n(exit 0)\n")
    if "review" in names:
        (folder / "round-1" / "review-code.md").write_text("VERDICT: PASS\n\n## Findings\nNone.\n")
    if "approve" in names:
        (folder / "decision.md").write_text("DECISION: APPROVE\n\n## Why\nEvery criterion is met.\n")


def publish(repo: Path, jobs_dir: Path) -> None:
    """Write the files the dashboard reads, and a stub CLI so the queue stays quiet."""
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
        "jobs": jobs,
    }
    (jobs_dir / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    (jobs_dir / "graph.json").write_text(json.dumps(graph_as_json(cfg), indent=1) + "\n")
    (jobs_dir / "config.json").write_text(json.dumps(config_as_json(cfg), indent=1) + "\n")
    binary: Path = repo / ".factory" / "bin" / "mfactory"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=4747)
    parser.add_argument("--empty", action="store_true", help="no jobs, to see the empty state")
    args = parser.parse_args()
    rng = random.Random(7)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "clinic-invoices"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / "factory.toml").write_text(CONFIG)
        jobs_dir = repo / ".factory" / "jobs"
        jobs_dir.mkdir(parents=True)
        if not args.empty:
            plan = [("done", d, None, d % 3 == 0) for d in (27, 24, 21, 20, 16, 13, 12, 9, 6, 5, 3, 1)]
            plan += [("rejected", 8, None, False), ("failed", 2, None, False), ("build", 0, None, False),
                     ("review", 0, None, True)]
            plan += [("spec-approval", 0, "maintainer", False), ("escalated", 1, "maintainer", True)]
            for number, (stop, days_ago, waiting, detour) in enumerate(plan, 1):
                make(jobs_dir, number, TITLES[(number - 1) % len(TITLES)], stop, days_ago, rng, waiting, detour)
        publish(repo, jobs_dir)
        serve_one(repo, args.port, open_browser=False)


if __name__ == "__main__":
    main()
