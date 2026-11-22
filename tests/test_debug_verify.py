"""The optional steps: a debugger after failed checks, and validation after merge or deploy."""

import subprocess

import pytest
from conftest import AUTO_MERGE
from modfactory import config
from modfactory.util import MARKER, ConfigError, Refused

DEBUG = "[steps.debug]\nenabled = true\n"


def failing_build(world, job):
    world.next(job)
    world.build(job, files={"FAIL": "x\n"})
    world.done(job)
    return world.next(job)


# ---- debug -----------------------------------------------------------------


def test_a_failed_check_goes_to_the_debugger_and_the_diagnosis_returns_to_the_builder(world):
    job = world.start(until="build", config_text=DEBUG)
    lines = failing_build(world, job)
    assert lines[0] == f"{job} #1: gate FAIL -> debug"
    assert lines[1] == "start-agent mfactory-debug"
    assert "Round 1, attempt 1." in lines[2]
    assert lines[2].split("Write: ")[1].startswith(f"{world.dir(job)}/round-1/debug-1.md.")
    world.debug(job, "FOUND")
    assert world.done(job) == [f"{job} #1: debug FOUND -> build"]
    prompt = world.next(job)[1]
    assert "Build round 1, attempt 2." in prompt
    assert "read round-1/checks.md" in prompt
    assert "The debugger's diagnosis is in round-1/debug-1.md." in prompt


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
    job = world.start(config_text=AUTO_MERGE + verify(commands=commands))
    j = world.job(job)
    assert j["step"] == "done" and j["history"][-1]["step"] == "verify"
    assert (world.dir(job) / "verify.md").read_text().startswith("VERIFY: PASS\n")
    assert f"COMMIT: {j['merge_sha']}" in (world.dir(job) / "verify-checks.md").read_text()
    assert not (world.repo / ".worktrees" / f"{job}-verify").exists()


def test_a_failing_verify_is_retried_then_fails_the_job_with_a_comment(world):
    job = world.start(config_text=AUTO_MERGE + verify(commands='["echo smoke broke; false"]', retries=1))
    j = world.job(job)
    assert j["step"] == "failed" and j["verify"]["attempt"] == 2
    comment = world.gh["prs"][str(j["pr"])]["comments"][-1]["body"]
    assert comment.startswith(f"{MARKER}\n### Factory: verification failed")
    assert "smoke broke" in comment
    assert world.next(job)[0].startswith(f"finished {job} #1: failed (The commands failed with exit 1.")
    assert (world.dir(job) / "verify.md").read_text().startswith("VERIFY: FAIL\n")


def test_on_failure_revert_pr_opens_a_draft_and_never_merges_it(world):
    job = world.start(config_text=AUTO_MERGE + verify(commands='["false"]', retries=0, on_failure='"revert-pr"'))
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
    job = world.start(until="merge", config_text=AUTO_MERGE + verify(
        after='"deploy"', wait=f'"test -f {marker}"', commands='["test $FACTORY_DEPLOY_URL = https://staging.test"]',
        url='"https://staging.test"', timeout_minutes=1))
    waiter = subprocess.Popen(f"sleep 0.3; touch {marker}", shell=True)
    world.next(job)
    waiter.wait()
    assert world.job(job)["step"] == "done"
    assert "Ran after deploy against https://staging.test" in (world.dir(job) / "verify-checks.md").read_text()


def test_the_verifier_agent_checks_after_the_commands_pass(world):
    job = world.start(until="verify", config_text=AUTO_MERGE + verify(commands='["true"]', agent="true"))
    lines = world.next(job)
    j = world.job(job)
    assert lines[0] == "start-agent mfactory-verify"
    assert f"Merge commit: {j['merge_sha']}. Checkout of that commit: {world.repo}/.worktrees/{job}-verify." in lines[1]
    with pytest.raises(Refused, match="verify.md wasn't written by the verifier agent"):
        world.done(job)
    world.verifier(job, "PASS")
    assert world.done(job) == [f"{job} #1: verify PASS -> done"]


def test_the_default_path_waits_then_a_hand_merge_is_verified(world):
    job = world.start(until="ready-to-merge", config_text=verify(commands='["true"]'))
    j = world.job(job)
    pr = str(j["pr"])
    assert j["running_since"] is None
    assert "or merge this pull request yourself; the factory will continue from there" in (
        world.gh["prs"][pr]["comments"][-1]["body"])
    assert world.gh["prs"][pr]["isDraft"] is False
    assert world.calls("pr ready")
    assert "factory:needs-human" in world.labels(1)
    assert world.queue() == [("wait", 1, job, "ready-to-merge", "waiting for a maintainer")]
    assert world.calls("pr merge") == []

    subprocess.run(["gh", "pr", "merge", pr, "--squash"], cwd=world.repo, check=True)
    lines = world.next(job)
    assert lines[0] == f"{job} #1: PR #{pr} was merged by hand -> verify"
    assert lines[1] == f"{job} #1: verify PASS -> done"
    finished = world.job(job)
    assert finished["merge_sha"]
    assert finished["history"][-2]["step"] == "ready-to-merge"
    assert finished["history"][-2]["outcome"] == "MERGED-BY-HAND"
    assert finished["history"][-2]["to"] == "verify"
    assert finished["history"][-1]["step"] == "verify"


def test_verify_needs_something_to_check_and_a_wait_for_deploys(world):
    with pytest.raises(ConfigError) as error:
        config.load(world.repo, text='schema = 1\n[steps.verify]\nenabled = true\nafter = "deploy"\n')
    assert error.value.problems == [
        "[steps.verify] is on but checks nothing: set commands, or agent = true",
        '[steps.verify].wait is missing: with after = "deploy" it is the command that exits 0 once the merge '
        "commit is live"]
