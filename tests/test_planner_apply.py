"""Approving, dismissing and applying planner findings: the only way a suggestion changes a tracker or a job."""

import json
import os
from pathlib import Path

import pytest
from modfactory.planner import apply
from modfactory.planner.state import PlannerState
from modfactory.util import Refused
from test_workspace_planner import launcher


def planner(world, action, tickets, kind="dependency"):
    """Scan, then ingest one finding with `action` between `tickets`. Returns the planner's state."""
    launcher(world.repo)
    assert world.cli("plan", "scan", "--force").returncode == 0
    draft = {"findings": [{"kind": kind, "tickets": tickets, "confidence": 0.9, "summary": "they belong together",
                           "evidence": [], "action": action}]}
    res = world.cli("plan", "ingest", "--reply", input=json.dumps(draft))
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("1 open findings"), res.stdout
    return PlannerState(Path(os.environ["FACTORY_HOME"]) / "planner")


def cli(world, *args, **kwargs):
    world.factory().store.release()  # the CLI takes the job's lock itself
    return world.cli(*args, **kwargs)


def status(state, n=1):
    return next(f for f in state.read_findings()["findings"] if f["id"] == f"F-{n}")


def two_issues(world):
    return world.issue("Add the VAT field to invoices"), world.issue("Show the VAT field on invoices")


def link_action(relation="blocks", a="repo:#1", b="repo:#2"):
    return {"type": "link", "from": a, "to": b, "relation": relation}


def test_link_github_comments_both_issues(world):
    two_issues(world)
    state = planner(world, link_action(), ["repo:#1", "repo:#2"])
    res = cli(world, "plan", "approve", "F-1")
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("will: link repo:#1 blocks repo:#2"), res.stdout
    assert any("Blocks #2" in c for c in world.comments(1))
    assert any("Blocked by #1" in c for c in world.comments(2))
    found = status(state)
    assert found["status"] == "approved" and found["decided_at"] and found["result"]


def test_link_jira_posts_issue_link(world):
    jira = world.use_jira()
    one, two = jira.ticket("Add the VAT field"), jira.ticket("Show the VAT field")
    planner(world, link_action("duplicate", f"repo:{one}", f"repo:{two}"), [f"repo:{one}", f"repo:{two}"])
    res = cli(world, "plan", "approve", "F-1")
    assert res.returncode == 0, res.stderr
    posted = [body for body in jira.links]
    assert len(posted) == 1 and posted[0]["type"] == {"name": "Duplicate"}
    assert posted[0]["outwardIssue"] == {"key": one} and posted[0]["inwardIssue"] == {"key": two}
    assert jira.comments(one) == [] and jira.comments(two) == []


def test_link_acli_comments(world):
    jira = world.use_acli()
    one, two = jira.ticket("Add the VAT field"), jira.ticket("Show the VAT field")
    planner(world, link_action("relates", f"repo:{one}", f"repo:{two}"), [f"repo:{one}", f"repo:{two}"])
    res = cli(world, "plan", "approve", "F-1")
    assert res.returncode == 0, res.stderr
    assert world.acli_calls("jira", "workitem", "comment", "create")
    assert jira.links == []
    assert any(f"Related to {two}" in c for c in jira.comments(one))


def test_close_duplicate_closes_and_links(world):
    world.issue("One")
    world.issue("Add the VAT field to invoices")
    three = world.issue("Add the VAT field to the invoices")
    job = world.create(three)
    action = {"type": "close-duplicate", "close": "repo:#3", "keep": "repo:#2"}
    planner(world, action, ["repo:#3", "repo:#2"], kind="duplicate")
    res = cli(world, "plan", "approve", "F-1")
    assert res.returncode == 0, res.stderr
    issue = world.gh["issues"]["3"]
    assert issue["state"] == "CLOSED" and issue["stateReason"] == "NOT_PLANNED"
    assert any("Duplicate of #2" in c for c in world.comments(3))
    assert world.job(job)["step"] == "done"
    assert world.gh["issues"]["2"]["state"] == "OPEN"


def test_close_duplicate_without_a_job(world):
    two_issues(world)
    action = {"type": "close-duplicate", "close": "repo:#2", "keep": "repo:#1"}
    planner(world, action, ["repo:#2", "repo:#1"], kind="duplicate")
    res = cli(world, "plan", "approve", "F-1")
    assert res.returncode == 0, res.stderr
    assert world.gh["issues"]["2"]["state"] == "CLOSED"
    assert any("Duplicate of #1" in c for c in world.comments(2))


def test_approve_hold_then_queue_waits(world):
    blocker, held = two_issues(world)
    action = {"type": "hold", "job": "repo:#2", "until": "repo:#1"}
    state = planner(world, action, ["repo:#1", "repo:#2"])
    res = cli(world, "plan", "approve", "F-1")
    assert res.returncode == 0, res.stderr
    assert status(state)["status"] == "approved"
    table = world.queue()
    assert ("wait", held, "-", "-", f"held until repo:#{blocker} is done") in table
    world.set_gh(lambda s: s["issues"][str(blocker)].update(labels=[]))     # the blocker leaves the queue
    assert [r[0] for r in world.queue() if r[1] == held] == ["new"]


def test_approve_context_names_the_planner_and_operator(world):
    job = world.start(until="spec")
    key = world.job(job)["issue"]
    other = world.issue("Other")
    action = {"type": "context", "job": f"repo:#{key}", "text": "Mind the cache."}
    planner(world, action, [f"repo:#{key}", f"repo:#{other}"], kind="overlap")
    res = cli(world, "plan", "approve", "F-1")
    assert res.returncode == 0, res.stderr
    text = (world.dir(job) / "context-1.md").read_text()
    assert "From the planner, approved by" in text and "Mind the cache." in text


def test_approve_comment(world):
    two_issues(world)
    action = {"type": "comment", "on": "repo:#2", "text": "See #1 first."}
    planner(world, action, ["repo:#2", "repo:#1"], kind="overlap")
    assert cli(world, "plan", "approve", "F-1").returncode == 0
    assert any("See #1 first." in c for c in world.comments(2))


def test_approve_refuses_unknown_or_decided(world):
    two_issues(world)
    state = planner(world, link_action(), ["repo:#1", "repo:#2"])
    missing = cli(world, "plan", "approve", "F-99")
    assert missing.returncode == 1 and "no finding F-99" in missing.stderr
    assert cli(world, "plan", "approve", "F-1").returncode == 0
    again = cli(world, "plan", "approve", "F-1")
    assert again.returncode == 1 and "already approved" in again.stderr
    assert len([c for c in world.comments(1) if "Blocks #2" in c]) == 1
    assert status(state)["status"] == "approved"
    dismissed = cli(world, "plan", "dismiss", "F-1")
    assert dismissed.returncode == 1 and "already approved" in dismissed.stderr


def test_failed_apply_keeps_finding_open(world):
    two_issues(world)
    state = planner(world, link_action(), ["repo:#1", "repo:#2"])
    world.set_gh(lambda s: s.update(fail="issue comment"))
    res = cli(world, "plan", "approve", "F-1")
    assert res.returncode == 1 and res.stderr.strip()
    found = status(state)
    assert found["status"] == "open" and found["decided_at"] is None and found["result"] is None


def test_dismiss(world):
    two_issues(world)
    state = planner(world, link_action(), ["repo:#1", "repo:#2"])
    res = cli(world, "plan", "dismiss", "F-1", "not related")
    assert res.returncode == 0, res.stderr
    found = status(state)
    assert found["status"] == "dismissed" and found["decided_at"] and "not related" in found["result"]
    again = cli(world, "plan", "dismiss", "F-1")
    assert again.returncode == 1 and "already dismissed" in again.stderr
    assert cli(world, "plan", "approve", "F-1").returncode == 1
    draft = {"findings": [{"kind": "dependency", "tickets": ["repo:#1", "repo:#2"], "confidence": 0.9,
                           "summary": "x", "evidence": [], "action": link_action()}]}
    assert cli(world, "plan", "ingest", "--reply", input=json.dumps(draft)).returncode == 0
    assert status(state)["status"] == "dismissed"
    assert world.comments(1) == []


def refs(world, *numbers):
    return {f"repo:#{n}": (world.repo, n) for n in numbers}


def test_apply_revalidates_what_it_is_given(world):
    two_issues(world)
    factory = world.factory()
    known = refs(world, 1, 2)
    bad = [
        {"type": "format-disk"},
        {"type": "link", "from": "repo:#1", "to": "repo:#2", "relation": "owns"},
        {"type": "link", "from": "repo:#1", "to": "repo:#1", "relation": "blocks"},
        {"type": "link", "from": "repo:#1", "to": "repo:#7", "relation": "blocks"},
        {"type": "link", "from": "repo:#1", "to": "repo:#2", "relation": "blocks", "extra": "x"},
        {"type": "comment", "on": "repo:#1", "text": 5},
        {"type": "comment", "on": "repo:#1", "text": "x" * 4001},
        {"type": "move", "job": "repo:#1", "step": "done"},
        {"type": "move", "job": "repo:#1", "step": ""},
        "not an object",
    ]
    for action in bad:
        with pytest.raises(Refused):
            apply.apply_action(factory, action, known)
    assert world.comments(1) == [] and world.comments(2) == []


def test_move_refuses_end_states_and_unknown_steps(world):
    job = world.start(until="spec")
    key = world.job(job)["issue"]
    factory = world.factory()
    known = {f"repo:#{key}": (world.repo, key)}
    for step in ("done", "rejected", "no-such-step"):
        with pytest.raises(Refused):
            apply.apply_action(factory, {"type": "move", "job": f"repo:#{key}", "step": step}, known)
    assert world.job(job)["step"] == "spec"


def test_plan_apply_takes_action_and_refs_as_json(world):
    two_issues(world)
    action = json.dumps(link_action("relates"))
    given = json.dumps({"repo:#1": {"project": str(world.repo), "key": 1, "url": "https://github.test/acme/app/issues/1"},
                        "repo:#2": {"project": str(world.repo), "key": 2, "url": ""}})
    res = cli(world, "plan", "apply", "--action", action, "--refs", given)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() and any("Related to #2" in c for c in world.comments(1))
    junk = cli(world, "plan", "apply", "--action", "{nope", "--refs", given)
    assert junk.returncode == 1


def test_link_to_another_project_uses_its_url(world, tmp_path):
    two_issues(world)
    factory = world.factory()
    known = {"repo:#1": (world.repo, 1), "beta:#9": (tmp_path / "beta", 9)}
    urls = {"repo:#1": "", "beta:#9": "https://github.test/acme/beta/issues/9"}
    apply.apply_action(factory, {"type": "link", "from": "repo:#1", "to": "beta:#9", "relation": "blocks"}, known, urls)
    assert any("Blocks https://github.test/acme/beta/issues/9" in c for c in world.comments(1))
    assert world.comments(2) == []


def test_describe_strips_control_characters_from_agent_text():
    from modfactory.planner.apply import describe
    line = describe({"type": "comment", "on": "repo:#1", "text": "hello \x1b[31mred\x07 world"})
    assert "\x1b" not in line and "\x07" not in line and "hello" in line and "world" in line
