"""`/planner.json`: the planner's open findings for the dashboard, redacted, with links filtered."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..planner import summary
from ..planner.apply import describe
from ..planner.ingest import SEVERITIES
from ..planner.state import PlannerState
from .feeds import text, web_url


def _ticket(ref: str, by_ref: dict[str, dict[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = by_ref.get(ref) or {}
    return {"ref": text(ref), "title": text(found.get("title")) or "", "url": web_url(found.get("url")),
            "priority": text(found.get("priority")) or "normal", "stage": text(found.get("stage")) or "",
            "project": text(found.get("project")) or ""}


def _finding(finding: dict[str, Any], by_ref: dict[str, dict[str, Any]]) -> dict[str, Any]:
    action: object = finding.get("action")
    evidence: object = finding.get("evidence")
    return {
        "id": text(finding.get("id")), "kind": text(finding.get("kind")),
        "severity": text(finding.get("severity")) or "low",
        "summary": text(finding.get("summary")) or "",
        "evidence": [text(e) for e in evidence if isinstance(e, str)] if isinstance(evidence, list) else [],
        "action": text(describe(action)) if isinstance(action, dict) else "",
        "tickets": [_ticket(ref, by_ref) for ref in finding.get("tickets") or [] if isinstance(ref, str)],
    }


def planner_payload(state: PlannerState, at: datetime | None = None) -> dict[str, Any]:
    """What `/planner.json` answers. A missing or corrupt findings file reads as no scan yet."""
    findings: dict[str, Any] = state.read_findings()
    snapshot: dict[str, Any] = state.read_snapshot() or {}
    tickets: object = snapshot.get("tickets")
    by_ref: dict[str, dict[str, Any]] = {
        t["ref"]: t for t in tickets if isinstance(t, dict) and isinstance(t.get("ref"), str)
    } if isinstance(tickets, list) else {}
    opened: list[dict[str, Any]] = [
        f for f in findings.get("findings") or [] if isinstance(f, dict) and f.get("status") == "open"]
    counts: dict[str, int] = {"open": len(opened)} | {level: 0 for level in SEVERITIES}
    for finding in opened:
        if finding.get("severity") in SEVERITIES:
            counts[str(finding["severity"])] += 1
    settings: dict[str, Any] = {"frequency": findings.get("frequency") or "medium"}
    scan: dict[str, Any] = summary._scan(snapshot, findings, settings, at or datetime.now(UTC))
    errors: object = snapshot.get("errors")
    return {
        "scanned_at": scan["scanned_at"],
        "alert_at": text(findings.get("alert_at")) or "high",
        "frequency": text(findings.get("frequency")) or "medium",
        "next_due": scan["next_due"],
        "counts": counts,
        "findings": [_finding(f, by_ref) for f in opened],
        "errors": [text(e) for e in errors if isinstance(e, str)] if isinstance(errors, list) else [],
    }
