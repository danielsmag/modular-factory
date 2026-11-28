"""One planner snapshot over every project in the workspace, with candidate pairs that cross projects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from subprocess import CompletedProcess, TimeoutExpired
from typing import Any

from ..graph import END_STATES
from ..planner import candidates
from ..planner.state import PlannerState
from ..util import run
from .project import Project
from .registry import Workspace

SNAPSHOT_TIMEOUT = 120
RELEASE_TIMEOUT = 60
TOO_OLD = "its factory is too old for the planner; run mfactory upgrade"


def _last_line(result: CompletedProcess[str], fallback: str) -> str:
    detail: list[str] = (result.stderr or result.stdout).strip().splitlines()
    return detail[-1] if detail else fallback


def _project_snapshot(project: Project, label: str) -> tuple[list[dict[str, Any]], list[str]]:
    """One project's tickets and errors. A project that cannot answer is an error, never a failure."""
    if not project.binary.is_file():
        return [], [f"{label}: .factory/bin/mfactory is missing"]
    try:
        result: CompletedProcess[str] = run(
            [str(project.binary), "plan", "snapshot", "--json", "--label", label], cwd=project.path, check=False,
            timeout=SNAPSHOT_TIMEOUT)
    except TimeoutExpired:
        return [], [f"{label}: timed out"]
    except OSError:
        return [], [f"{label}: .factory/bin/mfactory could not be run"]
    if result.returncode != 0:
        line: str = _last_line(result, "mfactory plan snapshot failed")
        return [], [f"{label}: {TOO_OLD if 'invalid choice' in line else line}"]
    try:
        parsed: Any = json.loads(result.stdout)
        tickets: list[dict[str, Any]] = [t for t in parsed["tickets"] if isinstance(t, dict)]
        problems: list[str] = [str(line) for line in parsed.get("errors") or []]
    except (ValueError, KeyError, TypeError, AttributeError):
        return [], [f"{label}: mfactory plan snapshot did not print a snapshot"]
    return tickets, problems


def _release(group: Workspace, tickets: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Release each hold whose awaited ticket, in another project, has finished. Returns refs and errors."""
    by_ref: dict[str, dict[str, Any]] = {t["ref"]: t for t in tickets}
    released: list[str] = []
    errors: list[str] = []
    for ticket in tickets:
        until: dict[str, Any] | None = by_ref.get(str(ticket.get("held_until")))
        if until is None or until["project"] == ticket["project"] or until["stage"] not in END_STATES:
            continue
        project: Project | None = group.project(ticket["project"])
        if project is None:
            continue
        try:
            result: CompletedProcess[str] = run(
                [str(project.binary), "plan", "release", str(ticket["key"])], cwd=project.path, check=False,
                timeout=RELEASE_TIMEOUT)
        except TimeoutExpired:
            errors.append(f"{ticket['project']}: timed out")
            continue
        except OSError:
            errors.append(f"{ticket['project']}: .factory/bin/mfactory could not be run")
            continue
        if result.returncode == 0:
            released.append(f"released {ticket['ref']}")
        else:
            errors.append(f"{ticket['project']}: could not release {ticket['ref']}: "
                          f"{_last_line(result, 'mfactory plan release failed')}")
    return released, errors


def collect(group: Workspace, settings: dict[str, Any], state: PlannerState) -> dict[str, Any]:
    """Snapshot every project, pair tickets across all of them, write and return the workspace snapshot."""
    tickets: list[dict[str, Any]] = []
    errors: list[str] = []
    labels: list[str] = []
    for project in group.projects:
        label: str = group.label(project)
        labels.append(label)
        found, problems = _project_snapshot(project, label)
        tickets += found
        errors += problems
    released, release_errors = _release(group, tickets)
    errors += release_errors
    digest: str = hashlib.sha256(",".join(sorted(f"{t['hash']}:{t['stage']}" for t in tickets)).encode()).hexdigest()
    snapshot: dict[str, Any] = {
        "schema": 1, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "hash": digest,
        "projects": labels, "tickets": tickets,
        "candidates": candidates.pairs(tickets, int(settings.get("max_pairs", 40))), "errors": errors,
        "released": released, "max_extra": int(settings.get("max_extra", 10))}
    state.write_snapshot(snapshot)
    return snapshot
