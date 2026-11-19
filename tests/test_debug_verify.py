"""The optional steps: a debugger after failed checks, and validation after merge or deploy."""

import subprocess

import pytest
from modfactory import config
from modfactory.util import MARKER, ConfigError, Refused

DEBUG = "[steps.debug]\nenabled = true\n"


def failing_build(world, job):
    world.next(job)
    world.build(job, files={"FAIL": "x\n"})
    world.done(job)
    return world.next(job)


# ---- debug -----------------------------------------------------------------


def test_failed_checks_go_to_the_debugger_first(world):
    job = world.start(until="build", config_text=DEBUG)
    lines = failing_build(world, job)
    assert lines[0] == f"{job} #1: gate FAIL -> debug"
    assert lines[1] == "start-agent factory-debug"
    assert "Round 1, attempt 1." in lines[2]
    assert lines[2].split("Write: ")[1].startswith(f"{world.dir(job)}/round-1/debug-1.md.")


def test_the_diagnosis_is_handed_to_the_builder(world):
    job = world.start(until="build", config_text=DEBUG)
    failing_build(world, job)
    world.debug(job, "FOUND")
    assert world.done(job) == [f"{job} #1: debug FOUND -> build"]
    prompt = world.next(job)[1]
    assert "Build round 1, attempt 2." in prompt
    assert "read round-1/checks.md" in prompt
    assert "The debugger's diagnosis is in round-1/debug-1.md." in prompt


def test_an_unknown_cause_still_returns_to_the_builder(world):
    job = world.start(until="build", config_text=DEBUG)
    failing_build(world, job)
    world.debug(job, "UNKNOWN")
    world.done(job)
    assert "The debugger could not find the cause; its notes are in round-1/debug-1.md." in world.next(job)[1]


def test_the_debugger_may_not_commit(world):
    job = world.start(until="build", config_text=DEBUG)
    failing_build(world, job)
    world.build(job, files={"FAIL": None})
    world.debug(job)
    with pytest.raises(Refused, match="debug agent changed the branch"):
        world.done(job)


def test_the_gate_limit_counts_debug_rounds(world):
    job = world.start(until="build", config_text=DEBUG + "[steps.gate]\non = { FAIL = { max = 1 } }\n")
    failing_build(world, job)
    world.debug(job)
    world.done(job)
    failing_build(world, job)
    j = world.job(job)
    assert j["step"] == "escalated" and j["reason"] == "[steps.gate].on.FAIL max = 1 reached"
    assert sorted(p.name for p in (world.dir(job) / "round-1").glob("debug-*")) == ["debug-1.md"]


# ---- verify ----------------------------------------------------------------


def verify(**settings):
    lines = ["[steps.verify]", "enabled = true"]
    lines += [f"{key} = {value}" for key, value in settings.items()]
    return "\n".join(lines) + "\n"


def test_verify_after_merge_runs_the_commands_on_the_merge_commit(world):
    commands = '["test -f feature-1-1.txt", "test $FACTORY_MERGE_SHA = $(git rev-parse HEAD)"]'
    job = world.start(config_text=verify(commands=commands))
    j = world.job(job)
    assert j["step"] == "done" and j["history"][-1]["step"] == "verify"
    assert (world.dir(job) / "verify.md").read_text().startswith("VERIFY: PASS\n")
    assert f"COMMIT: {j['merge_sha']}" in (world.dir(job) / "verify-checks.md").read_text()
    assert not (world.repo / ".worktrees" / f"{job}-verify").exists()


def test_a_failing_verify_is_retried_then_fails_the_job_with_a_comment(world):
    job = world.start(config_text=verify(commands='["echo smoke broke; false"]', retries=1))
    j = world.job(job)
    assert j["step"] == "failed" and j["verify"]["attempt"] == 2
    comment = world.gh["prs"][str(j["pr"])]["comments"][-1]["body"]
    assert comment.startswith(f"{MARKER}\n### Factory: verification failed")
    assert "smoke broke" in comment
    assert world.next(job)[0].startswith(f"finished {job} #1: failed (The commands failed with exit 1.")
    assert (world.dir(job) / "verify.md").read_text().startswith("VERIFY: FAIL\n")


def test_on_failure_issue_opens_a_new_factory_issue(world):
    job = world.start(config_text=verify(commands='["false"]', retries=0, on_failure='"issue"'))
    j = world.job(job)
    new = world.gh["issues"]["3"]
    assert new["title"] == f"Fix: verification failed after #{j['pr']} (Add a thing)"
    assert [label["name"] for label in new["labels"]] == ["factory"]
    assert "A new issue asks the factory to fix it" in world.gh["prs"][str(j["pr"])]["comments"][-1]["body"]
    assert ("new", 3, "-", "-", new["title"]) in world.queue()


def test_on_failure_revert_pr_opens_a_draft_and_never_merges_it(world):
    job = world.start(config_text=verify(commands='["false"]', retries=0, on_failure='"revert-pr"'))
    j = world.job(job)
    revert = world.gh["prs"]["3"]
    assert revert["isDraft"] and revert["state"] == "OPEN" and revert["headRefName"] == f"factory/revert-{job}"
    assert revert["title"] == f"Revert #{j['pr']}: Add a thing"
    files = subprocess.run(["git", "ls-tree", "--name-only", revert["headRefName"]], cwd=world.origin,
                           capture_output=True, text=True).stdout
    assert "feature-1-1.txt" not in files
    assert len(world.calls("pr merge")) == 1               # only the job's own pull request


def test_verify_after_deploy_waits_for_the_wait_command(world):
    marker = world.tmp / "deployed"
    job = world.start(until="merge", config_text=verify(
        after='"deploy"', wait=f'"test -f {marker}"', commands='["test $FACTORY_DEPLOY_URL = https://staging.test"]',
        url='"https://staging.test"', timeout_minutes=1))
    waiter = subprocess.Popen(f"sleep 0.3; touch {marker}", shell=True)
    world.next(job)
    waiter.wait()
    assert world.job(job)["step"] == "done"
    assert "Ran after deploy against https://staging.test" in (world.dir(job) / "verify-checks.md").read_text()


def test_verify_after_deploy_times_out(world, monkeypatch):
    job = world.start(until="merge", config_text=verify(
        after='"deploy"', wait='"false"', commands='["true"]', retries=0, timeout_minutes=1))
    monkeypatch.setattr("modfactory.scripted.time.monotonic", iter(range(0, 10_000, 40)).__next__)
    world.next(job)
    j = world.job(job)
    assert j["step"] == "failed" and "did not report commit" in j["reason"]


def test_the_verifier_agent_checks_after_the_commands_pass(world):
    job = world.start(until="verify", config_text=verify(commands='["true"]', agent="true"))
    lines = world.next(job)
    j = world.job(job)
    assert lines[0] == "start-agent factory-verify"
    assert f"Merge commit: {j['merge_sha']}. Checkout of that commit: {world.repo}/.worktrees/{job}-verify." in lines[1]
    with pytest.raises(Refused, match="verify.md wasn't written by the verifier agent"):
        world.done(job)
    world.verifier(job, "PASS")
    assert world.done(job) == [f"{job} #1: verify PASS -> done"]


def test_a_verifier_failure_is_retried_then_fails_with_its_findings(world):
    job = world.start(until="verify", config_text=verify(commands='["true"]', agent="true", retries=1))
    world.next(job)
    world.verifier(job, "FAIL")
    assert world.done(job) == [f"{job} #1: verify failed (attempt 1), trying again"]
    world.next(job)
    with pytest.raises(Refused, match="wasn't written by the verifier agent"):
        world.done(job)                                    # the first attempt's file doesn't count
    world.verifier(job, "FAIL")
    assert world.done(job) == [f"{job} #1: verify FAIL -> failed"]
    assert "1. It is missing." in world.gh["prs"][str(world.job(job)["pr"])]["comments"][-1]["body"]


def test_a_verifying_job_holds_no_slot(world):
    config = "[run]\nconcurrency = 1\n" + verify(commands='["true"]', agent="true")
    job = world.start(until="verify", config_text=config)
    assert world.job(job)["running_since"] is None
    other = world.create(world.issue(title="Second"))
    assert world.next(other)[0] == "start-agent factory-intake"


def test_a_pull_request_merged_by_hand_is_verified_too(world):
    job = world.start(until="approve", config_text=verify(commands='["test -f feature-1-1.txt"]'))
    world.next(job)
    world.approve(job, "ESCALATE")
    world.done(job)
    j = world.job(job)
    subprocess.run(["gh", "pr", "merge", str(j["pr"]), "--squash"], cwd=world.repo, check=True)
    lines = world.next(job)
    assert lines[0] == f"{job} #1: PR #{j['pr']} was merged by hand -> verify"
    assert lines[1] == f"{job} #1: verify PASS -> done"


def test_verify_needs_something_to_check_and_a_wait_for_deploys(world):
    with pytest.raises(ConfigError) as error:
        config.load(world.repo, text='schema = 1\n[steps.verify]\nenabled = true\nafter = "deploy"\n')
    assert error.value.problems == [
        "[steps.verify] is on but checks nothing: set commands, or agent = true",
        '[steps.verify].wait is missing: with after = "deploy" it is the command that exits 0 once the merge '
        "commit is live"]
