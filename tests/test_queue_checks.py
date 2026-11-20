"""What to run, how many at once, and what the checks protect."""

import json

import pytest
from modfactory.util import Refused

ONE = "[run]\nconcurrency = 1\n"


# ---- queue and concurrency -------------------------------------------------


def test_queue_lists_new_issues_in_order_up_to_the_free_slots(world):
    world.configure("[run]\nconcurrency = 2\n")
    for title in ("One", "Two", "Three"):
        world.issue(title=title)
    world.issue(title="Unlabeled", labels=())
    assert world.queue() == [
        ("new", 1, "-", "-", "One"), ("new", 2, "-", "-", "Two"), ("hold", 3, "-", "-", "concurrency 2 reached")]


def test_create_refuses_when_every_slot_is_taken(world):
    world.configure(ONE)
    first = world.create(world.issue(title="One"))
    with pytest.raises(Refused, match=f"concurrency 1 reached; running: {first}"):
        world.create(world.issue(title="Two"))


def test_a_waiting_job_frees_its_slot_and_queues_behind_the_others_when_it_returns(world):
    first = world.start(until="spec-approval", config_text=ONE + "[steps.spec-approval]\nenabled = true\n",
                        title="One")
    second = world.create(world.issue(title="Two"))
    assert world.queue() == [("resume", 2, second, "intake", "round 0"),
                             ("wait", 1, first, "spec-approval", "waiting for a maintainer")]

    world.comment(1, "/mfactory approve", "maintainer")
    rows = world.queue()
    assert ("hold", 1, first, "spec-approval", "concurrency 1 reached") in rows

    world.answer(first, "approve")                        # a person's reply is never refused
    assert world.next(first) == [f"hold {first} #1: waiting for a free slot (concurrency 1; running: {second})"]
    world.play(second)                                     # until it, too, waits for approval
    assert world.job(second)["step"] == "spec-approval"
    assert world.next(first)[0] == "start-agent mfactory-build"


def test_queue_explains_jobs_that_need_nothing(world):
    done = world.start(title="Done")
    rejected = world.create(world.issue(title="No"))
    world.next(rejected)
    world.intake(rejected, decision="REJECT")
    world.done(rejected)
    # The rejected issue lost its label. The merged one is still open here; on GitHub the merge closes it.
    assert world.queue() == [("skip", 1, done, "done", f"finished; delete .factory/jobs/{done} to run it again")]
    world.set_gh(lambda s: s["issues"]["1"].update(state="CLOSED"))
    assert world.queue() == []


def test_summary_is_a_table(world):
    job = world.start()
    lines = world.factory().summary([]).splitlines()
    assert lines[0].split() == ["job", "issue", "step", "round", "pr", "cost", "time"]
    assert lines[1].split() == [job, "#1", "done", "1", "#2", "-", "0m"]
    assert world.factory().summary(["1"]).splitlines()[1].startswith(job)


def test_two_commands_cannot_work_on_one_job(world):
    job = world.start(until="spec")
    res = world.cli("next", job)                            # this test's process still holds the job's lock
    assert res.returncode == 1 and f"another factory command is working on {job}" in res.stderr


def test_the_index_for_the_dashboard_follows_every_change(world):
    job = world.start(until="spec")
    index = json.loads((world.repo / ".factory/jobs/index.json").read_text())
    assert [j["id"] for j in index["jobs"]] == [job]
    assert index["jobs"][0]["step"] == "spec"
    assert ("intake.md", "DECISION: ACCEPT") in [(f["path"], f["first_line"]) for f in index["jobs"][0]["files"]]
    graph = json.loads((world.repo / ".factory/jobs/graph.json").read_text())
    assert graph["start"] == "intake" and graph["steps"][3] == {
        "name": "spec-approval", "type": "human", "enabled": False, "skip": "APPROVE", "who": "maintainer",
        "edges": [{"outcome": "APPROVE", "to": "build", "max": None},
                  {"outcome": "CHANGES", "to": "spec", "max": 3},
                  {"outcome": "REJECT", "to": "rejected", "max": None}]}


# ---- checks and protected paths -------------------------------------------


def gate(world, job, files):
    world.next(job)
    world.build(job, files=files)
    world.done(job)
    world.next(job)
    j = world.job(job)
    return j["gate"], (world.dir(job) / f"round-{j['round']}" / "checks.md").read_text()


def test_commands_run_in_order_and_stop_at_the_first_failure(world):
    config = '[checks]\ncommands = ["echo one", "echo two; exit 3", "echo three"]\n'
    job = world.start(until="build", config_text=config)
    verdict, report = gate(world, job, None)
    assert verdict == "FAIL" and "The checks failed with exit 3" in report
    log = (world.dir(job) / "round-1" / "checks.log").read_text()
    assert "$ echo one\none\n(exit 0)" in log and "(exit 3)" in log and "three" not in log


def test_no_commands_means_nothing_passes(world):
    job = world.start(until="build", config_text="[checks]\ncommands = []\n")
    verdict, _ = gate(world, job, None)
    assert verdict == "FAIL"
    assert "[checks].commands in factory.toml is empty" in (world.dir(job) / "round-1" / "checks.log").read_text()


@pytest.mark.parametrize("path", [
    "factory.toml", ".factory/roles/build.md", ".claude/agents/mfactory-approve.md",
    ".cursor/agents/mfactory-build.md", ".codex/agents/mfactory-build.toml", ".agents/skills/mfactory/SKILL.md"])
def test_a_job_cannot_change_the_factory_that_judges_it(world, path):
    job = world.start(until="build")
    verdict, report = gate(world, job, {path: "mine now\n"})
    assert verdict == "FAIL" and f"- {'M' if path == 'factory.toml' else 'A'} {path}" in report


def test_project_protected_paths_may_gain_files_but_not_lose_or_change_them(world):
    job = world.start(until="build", config_text='[paths]\nprotected = ["README.md", "docs/*"]\n')
    verdict, _ = gate(world, job, {"docs/new.md": "new\n"})
    assert verdict == "PASS"
    world.review(job, changes=["code"])
    world.done(job)
    verdict, report = gate(world, job, {"README.md": "changed\n"})
    assert verdict == "FAIL" and "- M README.md" in report


def test_the_checks_use_factory_toml_from_the_main_checkout(world):
    job = world.start(until="build")
    verdict, _ = gate(world, job, {"factory.toml": 'schema = 1\n[checks]\ncommands = ["true"]\n', "FAIL": "x\n"})
    assert verdict == "FAIL"                                # the branch's own, weaker checks were not used


def test_checks_time_out(world):
    job = world.start(until="build", config_text='[checks]\ncommands = ["sleep 5"]\n')
    world.next(job)
    world.build(job)
    world.done(job)
    factory = world.factory()
    factory.cfg["checks"]["timeout_minutes"] = 0.002     # the config only takes whole minutes
    factory.next(job)
    assert "(timed out after 0.002 minutes)" in (world.dir(job) / "round-1" / "checks.log").read_text()


def test_factory_checks_runs_them_where_you_are(world):
    assert world.cli("checks").stdout.endswith("checks: PASS in 0s\n")
    (world.repo / "FAIL").write_text("x")
    res = world.cli("checks")
    assert res.returncode == 1 and "checks: FAIL" in res.stdout
