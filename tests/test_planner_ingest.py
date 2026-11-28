"""The planner scan agent: validating its draft, severity, the merge with dismissals, the schedule and the CLI."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from modfactory.planner import ingest, schedule
from test_workspace_planner import launcher


def ticket(ref, priority="normal", stage="queued", job=None, hash_="aaaaaaaaaaaa"):
    return {"ref": ref, "project": ref.split(":")[0], "key": ref.split(":")[1], "tracker": "github", "title": ref,
            "excerpt": "", "labels": [], "priority": priority, "url": "", "stage": stage, "job": job, "branch": None,
            "files": [], "held_until": None, "hash": hash_}


def snap(*tickets, candidates=(), hash_="s1"):
    return {"schema": 1, "generated_at": "2026-01-01T00:00:00Z", "hash": hash_, "projects": ["shop"],
            "tickets": list(tickets), "candidates": [{"a": a, "b": b, "score": 0.9, "reasons": []}
                                                     for a, b in candidates], "errors": []}


def entry(a="shop:#1", b="shop:#2", kind="duplicate", action=None):
    return {"kind": kind, "tickets": [a, b], "confidence": 0.8, "summary": "same", "evidence": ["x"],
            "action": action or {"type": "link", "from": a, "to": b, "relation": "duplicate"}}


def test_severity_rules():
    queued = [ticket("shop:#1"), ticket("shop:#2")]
    building = [ticket("shop:#1", stage="build", job="001"), ticket("shop:#2", stage="build", job="002")]
    assert ingest.severity("duplicate", queued) == "medium"
    assert ingest.severity("duplicate", building) == "high"
    assert ingest.severity("overlap", queued) == "low"
    assert ingest.severity("overlap", [ticket("shop:#1", priority="high"), ticket("shop:#2")]) == "medium"
    urgent = [ticket("shop:#1", priority="critical", stage="build", job="001"), building[1]]
    assert ingest.severity("dependency", urgent) == "high"


def test_validate_drops_unknown_refs_and_actions():
    snapshot = snap(ticket("shop:#1"), ticket("shop:#2"), candidates=[("shop:#1", "shop:#2")])
    draft = {"findings": [
        entry(b="shop:#999"),
        entry(action={"type": "delete-repo"}),
        {**entry(), "tickets": ["shop:#1"]},
        entry()]}
    kept, dropped = ingest.validate(draft, snapshot, 5)
    assert len(kept) == 1 and kept[0]["tickets"] == ["shop:#1", "shop:#2"]
    assert len(dropped) == 3
    assert "shop:#999" in dropped[0] and "delete-repo" in dropped[1] and "2 distinct" in dropped[2]


def test_validate_drops_bad_confidence_text_and_junk():
    snapshot = snap(ticket("shop:#1"), ticket("shop:#2"), candidates=[("shop:#1", "shop:#2")])
    long_text = {"type": "comment", "on": "shop:#1", "text": "x" * 4001}
    draft = {"findings": [{**entry(), "confidence": 1.5}, entry(action=long_text), entry(kind="nope")]}
    kept, dropped = ingest.validate(draft, snapshot, 5)
    assert kept == [] and len(dropped) == 3
    assert ingest.validate("junk", snapshot, 5)[0] == []


def test_validate_caps_extra_pairs():
    tickets = [ticket(f"shop:#{n}") for n in range(1, 7)]
    snapshot = snap(*tickets)
    draft = {"findings": [entry(f"shop:#{n}", f"shop:#{n + 1}") for n in (1, 3, 5)]}
    kept, dropped = ingest.validate(draft, snapshot, 2)
    assert len(kept) == 2 and len(dropped) == 1


def merged(old, entries, snapshot, at="2026-01-01T00:00:00Z"):
    return ingest.merge(old, entries, snapshot, {"frequency": "medium", "alert_at": "high"}, at)


def test_merge_keeps_dismissal_until_ticket_changes():
    snapshot = snap(ticket("shop:#1"), ticket("shop:#2"))
    first = merged({"next_id": 1, "findings": []}, [entry()], snapshot)
    first["findings"][0]["status"] = "dismissed"
    again = merged(first, [entry()], snapshot, "2026-01-02T00:00:00Z")
    assert again["findings"][0]["status"] == "dismissed"
    changed = snap(ticket("shop:#1", hash_="bbbbbbbbbbbb"), ticket("shop:#2"))
    reopened = merged(again, [entry()], changed, "2026-01-03T00:00:00Z")
    found = reopened["findings"][0]
    assert found["status"] == "open" and found["id"] == "F-1"
    assert found["hashes"]["shop:#1"] == "bbbbbbbbbbbb" and found["updated_at"] == "2026-01-03T00:00:00Z"
    assert len(reopened["findings"]) == 1


def test_merge_numbers_new_findings():
    snapshot = snap(ticket("shop:#1"), ticket("shop:#2"), ticket("shop:#3"))
    first = merged({"next_id": 1, "findings": []}, [entry("shop:#1", "shop:#2")], snapshot)
    first["findings"][0]["status"] = "approved"
    second = merged(first, [entry("shop:#2", "shop:#3", kind="overlap")], snapshot)
    assert [f["id"] for f in second["findings"]] == ["F-1", "F-2"]
    assert second["findings"][0]["status"] == "approved" and second["next_id"] == 3
    third = merged(second, [], snapshot)
    assert [f["id"] for f in third["findings"]] == ["F-1"]  # the open one is dropped, the approved one stays
    assert third["snapshot_hash"] == "s1" and third["frequency"] == "medium"


def test_is_due():
    at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    stamp = "2026-01-01T12:00:00Z"
    scanned = {"scanned_at": stamp, "snapshot_hash": "h1"}
    assert not schedule.is_due("manual", {}, "h1", at)
    assert schedule.is_due("medium", {}, "h1", at)
    assert not schedule.is_due("medium", scanned, "h1", at + timedelta(minutes=60))
    assert not schedule.is_due("medium", scanned, "h2", at + timedelta(minutes=10))
    assert schedule.is_due("medium", scanned, "h2", at + timedelta(minutes=31))
    assert schedule.is_due("high", scanned, "h2", at + timedelta(minutes=6))


AGENT_LINES = "start-agent mfactory-planner-scan\nprompt: Planner scan. Snapshot: {snap}. Write: {draft}.\n" \
              "then: mfactory plan ingest\n"


def test_scan_prints_agent_lines_then_up_to_date(world):
    launcher(world.repo)
    world.issue("Add VAT field to the invoice API")
    world.issue("Show VAT field from invoice API")
    root = Path(os.environ["FACTORY_HOME"]) / "planner"
    lines = AGENT_LINES.format(snap=root / "snapshot.json", draft=root / "findings-draft.json")
    res = world.cli("plan", "scan")
    assert res.returncode == 0, res.stderr
    assert res.stdout == lines
    assert world.cli("plan", "due").stdout == "scan\n"
    tickets = json.loads((root / "snapshot.json").read_text())["tickets"]
    refs = [t["ref"] for t in tickets]
    assert len(refs) == 2
    (root / "findings-draft.json").write_text(json.dumps({"findings": [entry(refs[0], refs[1])]}))
    res = world.cli("plan", "ingest")
    assert res.stdout == "1 open findings (0 high)\n", res.stderr
    assert not (root / "findings-draft.json").exists()
    up = world.cli("plan", "scan").stdout
    assert up.startswith("up to date: nothing changed since the scan at ")
    assert world.cli("plan", "due").stdout == "not due\n"
    assert world.cli("plan", "scan", "--force").stdout == lines


def test_ingest_reply_reads_stdin_and_reports_dropped(world):
    launcher(world.repo)
    world.issue("Add VAT field to the invoice API")
    world.cli("plan", "scan")
    draft = json.dumps({"findings": [entry("repo:#1", "repo:#999")]})
    res = world.cli("plan", "ingest", "--reply", input="```json\n" + draft + "\n```")
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines()[0] == "0 open findings (0 high)"
    assert res.stdout.splitlines()[1].startswith("dropped: ") and "repo:#999" in res.stdout


def test_planner_scan_agent_is_generated(world):
    res = world.cli("sync")
    assert res.returncode == 0, res.stderr
    agent = (world.repo / ".claude" / "agents" / "mfactory-planner-scan.md").read_text()
    assert "model: haiku" in agent and "tools: Read, Grep, Glob, Write" in agent
    assert ".factory/roles/planner.md" in agent and "common.md" not in agent
    assert "planner-scan" in world.cli("models").stdout
    world.configure("[planner]\nenabled = false\n")
    world.cli("sync")
    assert not (world.repo / ".claude" / "agents" / "mfactory-planner-scan.md").exists()
