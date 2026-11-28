"""The planner's whole picture on demand: what needs attention, what is held, the pipeline and the last scan."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..graph import END_STATES
from ..util import parse_time
from . import apply as apply_mod
from . import schedule
from .ingest import KINDS, SEVERITIES
from .priority import LEVELS

SECTIONS: tuple[str, ...] = ("Needs attention", "Other findings", "Held", "Pipeline", "Last scan")
NO_SCAN = "No scan yet. Run /mfactory plan summary in your coding tool, or mfactory plan scan --force."
WAITING_LABELS: frozenset[str] = frozenset({"factory:needs-info", "factory:needs-human"})
URGENT: tuple[str, ...] = ("critical", "high")


def _project_of(ref: str) -> str:
    return ref.partition(":")[0]


def _number(finding_id: str) -> int:
    tail: str = finding_id.partition("-")[2]
    return int(tail) if tail.isdigit() else 0


def _level(ref: object, by_ref: dict[str, dict[str, Any]]) -> str:
    """A ticket's priority level; one the snapshot lacks counts as normal."""
    level: object = by_ref.get(str(ref), {}).get("priority")
    return str(level) if level in LEVELS else "normal"


def _top_priority(finding: dict[str, Any], by_ref: dict[str, dict[str, Any]]) -> int:
    """0 for a critical ticket down to 3 for low."""
    return min((LEVELS.index(_level(r, by_ref)) for r in finding.get("tickets", [])), default=LEVELS.index("normal"))


def _rank(finding: dict[str, Any], by_ref: dict[str, dict[str, Any]]) -> tuple[int, int, int]:
    severity: str = str(finding.get("severity"))
    level: int = SEVERITIES.index(severity) if severity in SEVERITIES else 0
    return -level, _top_priority(finding, by_ref), _number(str(finding.get("id")))


def _pipeline(label: str, tickets: list[dict[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {"label": label, "queued": 0, "running": 0, "waiting": 0, "held": 0, "by_step": {},
                           "urgent": []}
    for t in tickets:
        stage: str = str(t.get("stage"))
        if stage in END_STATES:
            continue
        if stage == "queued":
            row["queued"] += 1
        elif stage == "held":
            row["held"] += 1
        elif WAITING_LABELS & set(t.get("labels") or []):
            row["waiting"] += 1
        else:
            row["running"] += 1
            row["by_step"][stage] = row["by_step"].get(stage, 0) + 1
        if t.get("priority") in URGENT:
            row["urgent"].append({"ref": t["ref"], "title": t.get("title", ""), "priority": t["priority"],
                                  "stage": stage})
    return row


def _scan(snapshot: dict[str, Any], findings: dict[str, Any], settings: dict[str, Any], at: datetime) -> dict[str, Any]:
    scanned_at: object = findings.get("scanned_at")
    if not isinstance(scanned_at, str) or not scanned_at:
        return {"scanned_at": None, "changed_since": True, "next_due": None}
    changed: bool = findings.get("snapshot_hash") != snapshot.get("hash")
    next_due: str | None = None
    minutes: int | None = schedule.INTERVAL_MINUTES.get(str(settings.get("frequency", "medium")))
    if minutes is not None:
        try:
            next_due = (parse_time(scanned_at) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            next_due = None
    return {"scanned_at": scanned_at, "changed_since": changed, "next_due": next_due}


def build(snapshot: dict[str, Any], findings: dict[str, Any], settings: dict[str, Any], project: str | None,
          at: datetime) -> dict[str, Any]:
    """The report. With `project`, only that project's tickets, and the findings that touch it."""
    tickets: list[dict[str, Any]] = [t for t in snapshot.get("tickets", []) if isinstance(t, dict)]
    by_ref: dict[str, dict[str, Any]] = {t["ref"]: t for t in tickets}
    alert: str = str(settings.get("alert_at") or "high")
    floor: int = SEVERITIES.index(alert) if alert in SEVERITIES else SEVERITIES.index("high")
    opened: list[dict[str, Any]] = [
        f for f in findings.get("findings", []) if f.get("status") == "open" and (
            project is None or any(_project_of(str(r)) == project for r in f.get("tickets", [])))]
    opened = [{**f, "priorities": {str(r): _level(r, by_ref) for r in f.get("tickets", [])}} for f in opened]
    opened.sort(key=lambda f: _rank(f, by_ref))
    attention: list[dict[str, Any]] = []
    other: dict[str, list[dict[str, Any]]] = {}
    for f in opened:
        severity: str = str(f.get("severity"))
        if severity in SEVERITIES and SEVERITIES.index(severity) >= floor:
            attention.append(f)
        else:
            other.setdefault(str(f.get("kind")), []).append(f)
    other = {kind: other[kind] for kind in (*KINDS, *sorted(set(other) - set(KINDS))) if kind in other}
    since: dict[str, str | None] = {}
    for f in findings.get("findings", []):
        action: object = f.get("action")
        if f.get("status") == "approved" and isinstance(action, dict) and action.get("type") == "hold":
            since[str(action.get("job"))] = f.get("decided_at")
    held: list[dict[str, Any]] = [
        {"ref": t["ref"], "until": t.get("held_until"), "since": since.get(t["ref"])} for t in tickets
        if t.get("stage") == "held" and (project is None or t.get("project") == project)]
    labels: list[str] = [str(x) for x in snapshot.get("projects", [])]
    for t in tickets:
        if str(t.get("project")) not in labels:
            labels.append(str(t.get("project")))
    projects: list[dict[str, Any]] = [
        _pipeline(label, [t for t in tickets if t.get("project") == label])
        for label in labels if project is None or label == project]
    return {"attention": attention, "other": other, "held": held, "projects": projects,
            "scan": _scan(snapshot, findings, settings, at)}


def _line(finding: dict[str, Any]) -> list[str]:
    named: list[str] = [f"{r} ({finding.get('priorities', {}).get(r, 'normal')})" for r in finding["tickets"]]
    head: str = (f"{finding['id']} {finding['severity']} {finding['kind']}  {' ↔ '.join(named)}"
                 f" — {finding.get('summary', '')}")
    try:
        said: str = apply_mod.describe(finding["action"])
    except (KeyError, TypeError):
        said = str(finding.get("action"))
    return [head, f"  suggest: {said}  →  mfactory plan approve {finding['id']}"]


def render(report: dict[str, Any]) -> str:
    """Plain text, five headed sections. An empty section says `none`."""
    out: list[str] = []
    if not report["attention"] and not report["other"] and not report["held"]:
        out += ["Nothing to flag.", ""]
    out.append("Needs attention")
    for f in report["attention"]:
        out += _line(f)
    if not report["attention"]:
        out.append("  none")
    out += ["", "Other findings"]
    for group in report["other"].values():
        for f in group:
            out += _line(f)
    if not report["other"]:
        out.append("  none")
    out += ["", "Held"]
    for h in report["held"]:
        since: str = f", since {h['since']}" if h.get("since") else ""
        out.append(f"  {h['ref']} until {h['until']} is done{since}")
    if not report["held"]:
        out.append("  none")
    out += ["", "Pipeline"]
    for p in report["projects"]:
        steps: str = ", ".join(f"{step} {n}" for step, n in p["by_step"].items())
        out.append(f"  {p['label']}: {p['queued']} queued, {p['running']} running"
                   f"{f' ({steps})' if steps else ''}, {p['waiting']} waiting, {p['held']} held")
        for u in p["urgent"]:
            out.append(f"    {u['priority']}  {u['ref']}  {u['title']}  [{u['stage']}]")
    if not report["projects"]:
        out.append("  none")
    out += ["", "Last scan"]
    scan: dict[str, Any] = report["scan"]
    if not scan["scanned_at"]:
        out.append(f"  {NO_SCAN}")
    else:
        out.append(f"  {scan['scanned_at']}{', tickets have changed since' if scan['changed_since'] else ''}")
        if scan["next_due"]:
            out.append(f"  next due {scan['next_due']}")
    return "\n".join(out) + "\n"
