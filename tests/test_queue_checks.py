"""What to run, how many at once, and what the checks protect."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import AUTO_MERGE, World
from modfactory.util import Refused
from modfactory.workspace.jobs import read_jobs
from modfactory.workspace.project import Project

ONE = "[run]\nconcurrency = 1\n"


# ---- queue and concurrency -------------------------------------------------


def test_queue_lists_new_issues_in_order_up_to_the_free_slots(world):
    world.configure("[run]\nconcurrency = 2\n")
    for title in ("One", "Two", "Three"):
        world.issue(title=title)
    world.issue(title="Unlabeled", labels=())
    assert world.queue() == [
        ("new", 1, "-", "-", "One"), ("new", 2, "-", "-", "Two"), ("hold", 3, "-", "-", "concurrency 2 reached")]


def test_queue_json_adds_what_gh_issue_list_says_about_each_issue(world):
    number: int = world.issue(title="Add a widget")
    res = world.cli("queue", "--json")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == [{
        "action": "new", "issue": number, "job": "-", "step": "-", "note": "Add a widget",
        "title": "Add a widget", "issue_url": f"https://github.test/acme/app/issues/{number}",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
        "author": "alice", "labels": ["factory"],
    }]
    assert "note:" in res.stderr                            # the stale-files note stays out of the JSON
    assert len(world.calls("issue list")) == 1
    plain = world.cli("queue")
    assert plain.stdout.splitlines()[-1] == f"new  {number}  -  -  Add a widget"


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
    done = world.start(title="Done", config_text=AUTO_MERGE)
    rejected = world.create(world.issue(title="No"))
    world.next(rejected)
    world.intake(rejected, decision="REJECT")
    world.done(rejected)
    # The rejected issue lost its label. The merged one is still open here; on GitHub the merge closes it.
    assert world.queue() == [("skip", 1, done, "done", f"finished; mfactory delete {done} to run it again")]
    world.set_gh(lambda s: s["issues"]["1"].update(state="CLOSED"))
    assert world.queue() == []


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


def _release(world: World) -> None:
    if world._factory is not None:
        world._factory.store.release()


def _ref_exists(repo: Path, ref: str) -> bool:
    found: subprocess.CompletedProcess[str] = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref], cwd=repo, capture_output=True, text=True)
    return found.returncode == 0


def test_delete_removes_a_finished_job_and_rewrites_the_index(world):
    job: str = world.start(config_text=AUTO_MERGE)
    info: dict = world.job(job)
    issue: int = info["issue"]
    assert not Path(info["worktree"]).exists()
    _release(world)
    res = world.cli("delete", job)
    assert res.returncode == 0, res.stderr
    assert f"deleted {job} for #{issue}" in res.stdout
    assert f"note: issue #{issue} stays open; put the factory label back to run it again." in res.stdout
    assert not (world.repo / ".factory" / "jobs" / job).exists()
    index: dict = json.loads((world.repo / ".factory" / "jobs" / "index.json").read_text())
    assert job not in [row["id"] for row in index["jobs"]]
    assert world.gh["issues"][str(issue)]["state"] == "OPEN"


def test_delete_removes_a_rejected_job_after_its_pull_request_was_closed(world):
    job: str = world.start(until="ready-to-merge")
    assert world.job(job)["pr"] and not world.job(job)["merge_sha"]
    world.answer(job, "reject", "Not now.")
    rejected: dict = world.job(job)
    assert rejected["step"] == "rejected"
    assert rejected["pr"] and not rejected["merge_sha"]
    _release(world)
    res = world.cli("delete", job)
    assert res.returncode == 0, res.stderr
    assert not (world.repo / ".factory" / "jobs" / job).exists()


def test_delete_refuses_until_every_artifact_is_allowed(world):
    job: str = world.start(until="build")
    world.build(job)
    world.done(job)
    info: dict = world.job(job)
    branch: str = info["branch"]
    worktree: Path = Path(info["worktree"])
    folder: Path = world.repo / ".factory" / "jobs" / job
    number: int = info["pr"]
    assert worktree.exists()
    assert _ref_exists(world.repo, f"refs/heads/{branch}")
    assert _ref_exists(world.repo, f"refs/remotes/origin/{branch}")
    assert number and not info["merge_sha"]
    _release(world)

    refused = world.cli("delete", job)
    assert refused.returncode == 1
    for flag in ("--worktree", "--branch", "--remote", "--pr"):
        assert flag in refused.stderr
    assert "Nothing was removed" in refused.stderr
    assert worktree.exists() and folder.exists()
    assert world.gh["prs"][str(number)]["state"] == "OPEN"

    partial = world.cli("delete", job, "--worktree")
    assert partial.returncode == 1
    assert worktree.exists() and folder.exists()
    assert world.gh["prs"][str(number)]["state"] == "OPEN"
    assert not world.calls("pr close")

    forced = world.cli("delete", job, "--force")
    assert forced.returncode == 0, forced.stderr
    assert not worktree.exists() and not folder.exists()
    assert not _ref_exists(world.repo, f"refs/heads/{branch}")
    assert not _ref_exists(world.repo, f"refs/remotes/origin/{branch}")
    assert world.calls("pr close")
    assert world.gh["prs"][str(number)]["state"] == "CLOSED"
    index: dict = json.loads((world.repo / ".factory" / "jobs" / "index.json").read_text())
    assert job not in [row["id"] for row in index["jobs"]]
    assert world.gh["issues"][str(info["issue"])]["state"] == "OPEN"


def test_queue_rewrites_a_stale_index(world):
    job: str = world.start(until="spec")
    folder: Path = world.dir(job)
    index_path: Path = world.repo / ".factory" / "jobs" / "index.json"
    stale: str = index_path.read_text()
    _release(world)
    shutil.rmtree(folder)

    res = world.cli("queue")
    assert res.returncode == 0, res.stderr
    assert f"note: index.json dropped {job} (job folder gone)" in res.stdout
    index: dict = json.loads(index_path.read_text())
    assert job not in [row["id"] for row in index["jobs"]]

    index_path.write_text(stale)
    listed = world.cli("queue", "--json")
    assert listed.returncode == 0, listed.stderr
    assert f"note: index.json dropped {job} (job folder gone)" in listed.stderr
    assert isinstance(json.loads(listed.stdout), list)
    healed: dict = json.loads(index_path.read_text())
    assert job not in [row["id"] for row in healed["jobs"]]


def test_the_board_hides_a_job_whose_folder_is_gone(tmp_path: Path):
    repo: Path = tmp_path / "repo"
    jobs: Path = repo / ".factory" / "jobs"
    kept: Path = jobs / "002-keep"
    kept.mkdir(parents=True)
    (kept / "job.json").write_text(json.dumps({"id": "002-keep", "title": "Keep", "step": "spec"}))
    index: dict = {"schema": 1, "jobs": [
        {"id": "001-gone", "title": "Gone", "step": "intake"},
        {"id": "002-keep", "title": "Keep", "step": "spec"},
    ]}
    (jobs / "index.json").write_text(json.dumps(index))
    body: dict = read_jobs(Project(repo))
    assert body["error"] is None
    assert [row["id"] for row in body["jobs"]] == ["002-keep"]


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


def test_a_job_cannot_change_the_factory_that_judges_it(world):
    paths = (
        "factory.toml", ".factory/roles/build.md", ".claude/agents/mfactory-approve.md",
        ".cursor/agents/mfactory-build.md", ".codex/agents/mfactory-build.toml",
        ".agents/skills/mfactory/SKILL.md",
    )
    job = world.start(until="build")
    verdict, report = gate(world, job, {path: "mine now\n" for path in paths})
    assert verdict == "FAIL"
    assert "- M factory.toml" in report
    for path in paths[1:]:
        assert f"- A {path}" in report


def test_project_protected_paths_may_gain_files_but_not_lose_or_change_them(world):
    job = world.start(until="build", config_text='[paths]\nprotected = ["README.md", "docs/*"]\n')
    verdict, _ = gate(world, job, {"docs/new.md": "new\n"})
    assert verdict == "PASS"
    world.review(job, changes=["code"])
    world.done(job)
    verdict, report = gate(world, job, {"README.md": "changed\n"})
    assert verdict == "FAIL" and "- M README.md" in report


def test_checks_get_the_main_checkout_path(world):
    job: str = world.start(until="build", config_text='[checks]\ncommands = ["printf %s \\"$FACTORY_CHECKOUT\\""]\n')
    verdict, _report = gate(world, job, None)
    log: str = (world.dir(job) / "round-1" / "checks.log").read_text()
    assert verdict == "PASS"
    assert str(world.repo) in log


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



def test_the_queue_offers_only_the_issues_pick_authors_and_assignees_name(world):
    world.configure("[run]\nconcurrency = 9\n")
    job = world.create(world.issue(title="Started", author="alice"))
    world.issue(title="Alice's", author="alice")
    world.issue(title="Mine", author="operator")
    world.issue(title="Assigned to me", author="alice", assignees=("operator",))
    world.issue(title="Carol's", author="carol")
    assert [row[4] for row in world.queue()][1:] == ["Alice's", "Mine", "Assigned to me", "Carol's"]

    world.configure('[run]\nconcurrency = 9\npick = "mine"\nauthors = ["carol"]\n')
    assert world.queue() == [("resume", 1, job, "intake", "round 0"),      # a started job keeps its place
                             ("new", 3, "-", "-", "Mine"), ("new", 4, "-", "-", "Assigned to me"),
                             ("new", 5, "-", "-", "Carol's")]
    assert world.job(world.create(2))["issue"] == 2                         # by number, any labeled issue runs
