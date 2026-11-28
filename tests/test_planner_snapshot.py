"""The planner's snapshot of a project's tickets, the candidate pairs, and the state files."""

import json

from modfactory.planner import candidates
from modfactory.planner.state import PlannerState


def snapshot(world, *extra):
    res = world.cli("plan", "snapshot", "--json", *extra)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def ticket(ref, title, body="", labels=(), files=(), stage="queued", project="shop"):
    return {"ref": f"{project}:{ref}", "project": project, "title": title, "excerpt": body,
            "labels": list(labels), "files": list(files), "stage": stage}


def test_snapshot_lists_queued_issues_and_jobs(world):
    first = world.issue("Add a thing", labels=("factory", "P1"))
    world.issue("Another thing")
    world.create(first)
    out = snapshot(world, "--label", "shop")
    assert out["label"] == "shop" and out["errors"] == []
    tickets = {t["ref"]: t for t in out["tickets"]}
    assert sorted(tickets) == ["shop:#1", "shop:#2"]
    assert tickets["shop:#2"]["stage"] == "queued"
    assert tickets["shop:#1"]["stage"] != "queued" and tickets["shop:#1"]["job"]
    assert tickets["shop:#1"]["priority"] == "high"
    assert tickets["shop:#2"]["priority"] == "normal"
    assert snapshot(world)["label"] == "repo"


def test_snapshot_reads_branch_files(world):
    job = world.start(until="build")
    world.build(job, files={"app/money.py": "x = 1\n"})
    world.done(job)
    world.play(job, until="review")
    out = snapshot(world)
    assert [t["files"] for t in out["tickets"]] == [["app/money.py"]]


def test_snapshot_jira_and_both(world):
    jira = world.use_both()
    world.issue("From GitHub")
    card = jira.ticket("From Jira", "Body", labels=[])
    out = snapshot(world)
    by_tracker = {t["tracker"]: t for t in out["tickets"]}
    assert set(by_tracker) == {"github", "jira"}
    assert by_tracker["jira"]["key"] == card
    assert by_tracker["jira"]["priority"] in {"critical", "high", "normal", "low"}
    assert by_tracker["jira"]["ref"] == f"repo:{card}"


def test_pairs_similar_titles():
    found = candidates.pairs([
        ticket("#1", "Export invoices as CSV"), ticket("#2", "Allow CSV export of invoices"),
        ticket("#3", "Dark mode")], 40)
    assert [(p["a"], p["b"]) for p in found] == [("shop:#1", "shop:#2")]
    assert "similar text" in found[0]["reasons"]


def test_pairs_mentions_and_files():
    found = candidates.pairs([
        ticket("#4", "Alpha", "zzz"), ticket("#5", "Beta", "depends on #4"),
        ticket("#6", "Gamma", files=["a.py"], stage="build"), ticket("#7", "Delta", files=["a.py"], stage="build"),
    ], 40)
    by = {(p["a"], p["b"]): p for p in found}
    assert "mentions shop:#4" in by[("shop:#4", "shop:#5")]["reasons"]
    assert by[("shop:#4", "shop:#5")]["score"] == 0.9
    assert "same files: a.py" in by[("shop:#6", "shop:#7")]["reasons"]
    assert by[("shop:#6", "shop:#7")]["score"] == 0.6


def test_pairs_skip_finished_and_references():
    assert candidates.references("see #4 and PROJ-7, not abc-1") == {"#4", "PROJ-7"}
    found = candidates.pairs([ticket("#1", "Same words here", stage="done"),
                              ticket("#2", "Same words here", stage="rejected")], 40)
    assert found == []


def test_pairs_capped_and_sorted():
    tickets = [ticket(f"#{n}", "Export invoices as CSV file", "mentions" if n % 2 else "other") for n in range(30)]
    found = candidates.pairs(tickets, 5)
    assert len(found) == 5
    scores = [p["score"] for p in found]
    assert scores == sorted(scores, reverse=True)


def test_state_read_findings_tolerates_corrupt_file(tmp_path):
    state = PlannerState(tmp_path / "planner")
    assert state.read_snapshot() is None
    assert state.read_findings()["findings"] == []
    state.write_snapshot({"schema": 1})
    assert state.read_snapshot() == {"schema": 1}
    state.findings_path.write_text("{")
    assert state.read_findings()["findings"] == []
    state.write_findings({"schema": 1, "findings": [1]})
    assert state.read_findings()["findings"] == [1]
