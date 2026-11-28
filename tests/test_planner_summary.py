"""`plan summary`, `plan show` and `plan suggest`: the planner's findings on demand, and the skill that drives them."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from modfactory.planner import summary
from modfactory.planner.state import PlannerState
from test_planner_ingest import entry, snap, ticket
from test_workspace_planner import launcher, pair  # noqa: F401  (pair is a fixture)

AT = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
SETTINGS = {"frequency": "medium", "alert_at": "high"}


def finding(n, kind="duplicate", severity="high", tickets=("shop:#1", "shop:#2"), status="open"):
    a, b = tickets
    return {"id": f"F-{n}", "key": f"{kind}|{a},{b}", "kind": kind, "tickets": [a, b], "severity": severity,
            "confidence": 0.9, "summary": f"summary {n}", "evidence": [], "status": status,
            "action": {"type": "link", "from": a, "to": b, "relation": "duplicate"}, "hashes": {},
            "created_at": "t", "updated_at": "t", "decided_at": None, "result": None}


def findings_file(*found, scanned_at="2026-01-01T00:30:00Z", hash_="s1"):
    return {"schema": 1, "scanned_at": scanned_at, "snapshot_hash": hash_, "frequency": "medium",
            "alert_at": "high", "next_id": len(found) + 1, "findings": list(found)}


def test_summary_sections_in_order():
    held = ticket("shop:#3", stage="held")
    held["held_until"] = "api:PROJ-7"
    snapshot = snap(ticket("shop:#1", priority="high"), ticket("shop:#2"), held)
    report = summary.build(snapshot, findings_file(finding(1), finding(2, "overlap", "low")), SETTINGS, None, AT)
    text = summary.render(report)
    heads = ["Needs attention", "Other findings", "Held", "Pipeline", "Last scan"]
    places = [text.index(h) for h in heads]
    assert places == sorted(places)
    assert "F-1 high duplicate  shop:#1 (high) ↔ shop:#2 (normal) — summary 1" in text
    assert "  suggest: " in text and "  →  mfactory plan approve F-1" in text
    assert "shop:#3" in text and "api:PROJ-7" in text
    assert not text.startswith("Nothing to flag.")


def test_attention_sorted_by_severity_then_priority():
    snapshot = snap(ticket("shop:#1"), ticket("shop:#2"), ticket("shop:#3", priority="critical"), ticket("shop:#4"))
    found = [finding(1, tickets=("shop:#1", "shop:#2")), finding(2, tickets=("shop:#3", "shop:#4")),
             finding(3, "overlap", "medium", ("shop:#1", "shop:#4"))]
    report = summary.build(snapshot, findings_file(*found), SETTINGS, None, AT)
    assert [f["id"] for f in report["attention"]] == ["F-2", "F-1"]
    assert [f["id"] for f in report["other"]["overlap"]] == ["F-3"]
    text = summary.render(report)
    assert text.index("F-2") < text.index("F-1") < text.index("Other findings") < text.index("F-3")


def test_summary_pipeline_counts():
    snapshot = snap(ticket("shop:#1", priority="critical"), ticket("shop:#2", stage="build", job="001"),
                    ticket("shop:#3", stage="done", job="002"), ticket("api:PROJ-1", stage="held", job="003"))
    report = summary.build(snapshot, findings_file(), SETTINGS, "shop", AT)
    assert [p["label"] for p in report["projects"]] == ["shop"]
    row = report["projects"][0]
    assert (row["queued"], row["running"], row["held"], row["by_step"]) == (1, 1, 0, {"build": 1})
    assert row["urgent"] == [{"ref": "shop:#1", "title": "shop:#1", "priority": "critical", "stage": "queued"}]


def test_summary_empty_and_no_scan(world):
    launcher(world.repo)
    res = world.cli("plan", "summary")
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("Nothing to flag.")
    assert "No scan yet. Run /mfactory plan summary in your coding tool, or mfactory plan scan --force." in res.stdout
    for head in ("Needs attention", "Other findings", "Held", "Pipeline", "Last scan"):
        assert head in res.stdout
    assert "none" in res.stdout


def scan_and_ingest(world, *refs):
    assert world.cli("plan", "scan", "--force").returncode == 0
    draft = {"findings": [entry(a, b) for a, b in refs]}
    res = world.cli("plan", "ingest", "--reply", input=json.dumps(draft))
    assert res.returncode == 0, res.stderr


def test_summary_project_filter_and_json(pair):  # noqa: F811
    pair.a.issue("Add the VAT field to invoices")
    with pair.in_b() as b:
        b.issue("Show the VAT field on invoices")
        b.issue("Show the VAT total on invoices")
    scan_and_ingest(pair.a, ("repo:#1", "beta:#1"), ("beta:#1", "beta:#2"))
    out = json.loads(pair.a.cli("plan", "summary", "--project", "beta", "--json").stdout)
    assert [p["label"] for p in out["projects"]] == ["beta"]
    assert len(out["attention"]) + sum(len(v) for v in out["other"].values()) == 2
    only = json.loads(pair.a.cli("plan", "summary", "--project", "repo", "--json").stdout)
    assert [p["label"] for p in only["projects"]] == ["repo"]
    shown = [f["tickets"] for f in only["attention"]] + [f["tickets"] for v in only["other"].values() for f in v]
    assert shown == [["repo:#1", "beta:#1"]]
    assert set(out) == {"attention", "other", "held", "projects", "scan"}


def test_summary_fresh_prints_scan_lines_only_when_changed(world):
    world.issue("Add the VAT field to invoices")
    world.issue("Show the VAT field on invoices")
    launcher(world.repo)
    scan_and_ingest(world, ("repo:#1", "repo:#2"))
    res = world.cli("plan", "summary", "--fresh")
    assert res.returncode == 0, res.stderr
    assert "start-agent" not in res.stdout and "Needs attention" in res.stdout
    world.set_gh(lambda state: state["issues"]["1"].update(title="Add the VAT line to invoices"))
    res = world.cli("plan", "summary", "--fresh")
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert lines[0] == "start-agent mfactory-planner-scan"
    assert lines[1].startswith("prompt: Planner scan.")
    assert lines[2] == "then: mfactory plan summary" and len(lines) == 3


def state_of(world):
    return PlannerState(Path(os.environ["FACTORY_HOME"]) / "planner")


def test_suggest_adds_open_finding(world):
    world.issue("Add the VAT field to invoices")
    world.issue("Show the VAT field on invoices")
    world.issue("Rework the login page")
    launcher(world.repo)
    scan_and_ingest(world, ("repo:#1", "repo:#2"))
    before = state_of(world).read_findings()
    assert world.cli("plan", "dismiss", "F-1").returncode == 0
    new = {"findings": [entry("repo:#1", "repo:#3", kind="overlap")]}
    res = world.cli("plan", "suggest", input=json.dumps(new))
    assert res.returncode == 0, res.stderr
    shown = json.loads(world.cli("plan", "show", "--json").stdout)
    by_id = {f["id"]: f for f in shown["findings"]}
    assert by_id["F-2"]["status"] == "open" and by_id["F-2"]["tickets"] == ["repo:#1", "repo:#3"]
    assert by_id["F-1"]["status"] == "dismissed"
    assert shown["scanned_at"] == before["scanned_at"] and shown["snapshot_hash"] == before["snapshot_hash"]
    text = world.cli("plan", "show")
    assert "F-2" in text.stdout and "open" in text.stdout


def test_suggest_keeps_other_open_findings(world):
    world.issue("Add the VAT field to invoices")
    world.issue("Show the VAT field on invoices")
    world.issue("Rework the login page")
    launcher(world.repo)
    scan_and_ingest(world, ("repo:#1", "repo:#2"))
    new = {"findings": [entry("repo:#1", "repo:#3", kind="overlap")]}
    assert world.cli("plan", "suggest", input=json.dumps(new)).returncode == 0
    shown = json.loads(world.cli("plan", "show", "--json").stdout)
    assert [(f["id"], f["status"]) for f in shown["findings"]] == [("F-1", "open"), ("F-2", "open")]


def test_suggest_rejects_an_invalid_entry(world):
    world.issue("Add the VAT field to invoices")
    launcher(world.repo)
    assert world.cli("plan", "scan", "--force").returncode == 0
    bad = {"findings": [entry("repo:#1", "repo:#99")]}
    res = world.cli("plan", "suggest", input=json.dumps(bad))
    assert res.returncode == 1
    assert "dropped:" in res.stdout and "repo:#99" in res.stdout
    assert json.loads(world.cli("plan", "show", "--json").stdout)["findings"] == []


def test_skill_mentions_planner_commands(world):
    assert world.cli("sync").returncode == 0
    text = (world.repo / ".claude/skills/mfactory/SKILL.md").read_text()
    for needle in ("mfactory plan summary --fresh", "mfactory plan due", "only when the user says so",
                   "mfactory plan suggest", "/mfactory plan summary"):
        assert needle in text, needle
