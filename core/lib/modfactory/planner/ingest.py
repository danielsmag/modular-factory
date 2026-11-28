"""Checking the scan agent's draft and merging it into the findings, with the memory of what was dismissed."""

from __future__ import annotations

from typing import Any

from ..graph import END_STATES

KINDS: tuple[str, ...] = ("duplicate", "dependency", "overlap", "conflict", "priority-inversion")
ACTIONS: tuple[str, ...] = ("link", "hold", "close-duplicate", "context", "move", "comment")
SEVERITIES: tuple[str, ...] = ("low", "medium", "high")
BASE: dict[str, str] = {"conflict": "high", "duplicate": "medium", "dependency": "medium",
                        "priority-inversion": "medium", "overlap": "low"}
# The action fields that name a ticket; every other string field of an action is text.
REF_FIELDS: dict[str, tuple[str, ...]] = {
    "link": ("from", "to"), "hold": ("job", "until"), "close-duplicate": ("close", "keep"),
    "context": ("job",), "move": ("job",), "comment": ("on",)}
MAX_TEXT = 4000


def _raise(level: str) -> str:
    return SEVERITIES[min(SEVERITIES.index(level) + 1, len(SEVERITIES) - 1)]


def severity(kind: str, tickets: list[dict[str, Any]]) -> str:
    """The kind's base level, one higher for an urgent ticket, one more when every ticket has a running job."""
    level: str = BASE.get(kind, "low")
    if any(t.get("priority") in ("critical", "high") for t in tickets):
        level = _raise(level)
    if tickets and all(t.get("job") and t.get("stage") not in END_STATES for t in tickets):
        level = _raise(level)
    return level


def _pair_key(refs: list[str]) -> frozenset[str]:
    return frozenset(refs)


def _entry_problem(entry: object, known: set[str]) -> str | None:
    """Why this entry cannot be kept, or None."""
    if not isinstance(entry, dict):
        return "an entry is not an object"
    kind: object = entry.get("kind")
    if kind not in KINDS:
        return f"kind {kind!r} is not one of {', '.join(KINDS)}"
    refs: object = entry.get("tickets")
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        return f"{kind}: tickets must be a list of refs"
    if len(set(refs)) != 2 or len(refs) != 2:
        return f"{kind}: needs 2 distinct tickets, got {len(set(refs))}"
    for ref in refs:
        if ref not in known:
            return f"{kind}: {ref} is not in the snapshot"
    confidence: object = entry.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
        return f"{kind} {' '.join(refs)}: confidence {confidence!r} is not between 0 and 1"
    action: object = entry.get("action")
    if not isinstance(action, dict):
        return f"{kind} {' '.join(refs)}: the action is missing"
    kind_of_action: object = action.get("type")
    if kind_of_action not in ACTIONS:
        return f"action type {kind_of_action!r} is not one of {', '.join(ACTIONS)}"
    names: tuple[str, ...] = REF_FIELDS[str(kind_of_action)]
    for name in names:
        value: object = action.get(name)
        if not isinstance(value, str) or value not in known:
            return f"action {kind_of_action}: {name} {value!r} is not in the snapshot"
    for name, value in action.items():
        if name in names or name == "type":
            continue
        if isinstance(value, str) and len(value) > MAX_TEXT:
            return f"action {kind_of_action}: {name} is longer than {MAX_TEXT} characters"
    return None


def validate(draft: object, snapshot: dict[str, Any], max_extra: int) -> tuple[list[dict[str, Any]], list[str]]:
    """The entries of a draft that are safe to keep, and a reason for each one dropped."""
    entries: object = draft.get("findings") if isinstance(draft, dict) else None
    if not isinstance(entries, list):
        return [], ["the draft is not an object with a findings list"]
    known: set[str] = {t["ref"] for t in snapshot.get("tickets", [])}
    candidates: set[frozenset[str]] = {_pair_key([c["a"], c["b"]]) for c in snapshot.get("candidates", [])}
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    extra = 0
    for entry in entries:
        problem: str | None = _entry_problem(entry, known)
        if problem is None and _pair_key(entry["tickets"]) not in candidates:
            if extra >= max_extra:
                problem = f"{entry['kind']} {' '.join(entry['tickets'])}: more than {max_extra} extra pairs"
            else:
                extra += 1
        if problem is not None:
            dropped.append(problem)
            continue
        evidence: object = entry.get("evidence")
        kept.append({
            "kind": entry["kind"], "tickets": list(entry["tickets"]), "confidence": float(entry["confidence"]),
            "summary": str(entry.get("summary") or ""),
            "evidence": [str(e) for e in evidence] if isinstance(evidence, list) else [],
            "action": dict(entry["action"])})
    return kept, dropped


def finding_key(kind: str, refs: list[str]) -> str:
    return f"{kind}|{','.join(sorted(refs))}"


def merge(old: dict[str, Any], kept: list[dict[str, Any]], snapshot: dict[str, Any], settings: dict[str, Any],
          at: str) -> dict[str, Any]:
    """The new findings file. A dismissed finding stays dismissed until one of its tickets changes."""
    by_ref: dict[str, dict[str, Any]] = {t["ref"]: t for t in snapshot.get("tickets", [])}
    fresh: dict[str, dict[str, Any]] = {finding_key(e["kind"], e["tickets"]): e for e in kept}
    next_id: int = int(old.get("next_id") or 1)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def fill(entry: dict[str, Any]) -> dict[str, Any]:
        tickets: list[dict[str, Any]] = [by_ref[r] for r in entry["tickets"]]
        return {"kind": entry["kind"], "tickets": entry["tickets"],
                "severity": severity(entry["kind"], tickets), "confidence": entry["confidence"],
                "summary": entry["summary"], "evidence": entry["evidence"], "action": entry["action"],
                "hashes": {t["ref"]: t["hash"] for t in tickets}}

    for finding in old.get("findings", []):
        key: str = str(finding.get("key"))
        entry: dict[str, Any] | None = fresh.get(key)
        status: object = finding.get("status")
        if status == "approved":
            out.append(finding)
            seen.add(key)
        elif status == "dismissed":
            if entry is None:
                out.append(finding)
                continue
            seen.add(key)
            new: dict[str, Any] = fill(entry)
            if new["hashes"] == finding.get("hashes"):
                out.append(finding)
            else:
                out.append({**finding, **new, "status": "open", "updated_at": at, "decided_at": None,
                            "result": None})
        elif entry is not None and key not in seen:
            seen.add(key)
            out.append({**finding, **fill(entry), "updated_at": at})
    for key, entry in fresh.items():
        if key in seen:
            continue
        out.append({"id": f"F-{next_id}", "key": key, **fill(entry), "status": "open", "created_at": at,
                    "updated_at": at, "decided_at": None, "result": None})
        next_id += 1
    return {"schema": 1, "scanned_at": at, "snapshot_hash": snapshot.get("hash"),
            "frequency": settings.get("frequency"), "alert_at": settings.get("alert_at"), "next_id": next_id,
            "findings": out}
