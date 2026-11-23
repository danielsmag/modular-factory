"""The optional steps: a debugger after failed checks, and validation after merge or deploy."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import AUTO_MERGE, World
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
    checkout: str = str(world.repo)
    commands: str = (
        '["test -f feature-1-1.txt", "test $FACTORY_MERGE_SHA = $(git rev-parse HEAD)", '
        f'"test \\"$FACTORY_CHECKOUT\\" = \\"{checkout}\\""]'
    )
    job = world.start(config_text=AUTO_MERGE + verify(commands=commands))
    j = world.job(job)
    log: str = (world.dir(job) / "verify-checks.log").read_text()
    assert j["step"] == "done" and j["history"][-1]["step"] == "verify"
    merged = [h for h in j["history"] if h["step"] == "merge"][-1]
    assert merged["to"] == "verify" and merged["skipped"] == ["ready-to-verify"]
    assert (world.dir(job) / "verify.md").read_text().startswith("VERIFY: PASS\n")
    assert f"COMMIT: {j['merge_sha']}" in (world.dir(job) / "verify-checks.md").read_text()
    assert checkout in log and "(exit 0)" in log
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


def test_a_revert_branch_uses_the_configured_prefix(world: World) -> None:
    world.configure(AUTO_MERGE + verify(commands='["false"]', retries=0, on_failure='"revert-pr"'))
    path: Path = world.repo / "factory.toml"
    text: str = path.read_text().replace(
        'base_branch = "main"\n', 'base_branch = "main"\nbranch_prefix = "feature/"\n')
    path.write_text(text)
    job: str = world.create(world.issue())
    world.play(job)
    assert world.job(job)["branch"] == f"feature/{job}"
    revert: dict[str, Any] = world.gh["prs"]["3"]
    assert revert["isDraft"] and revert["headRefName"] == f"feature/revert-{job}"


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


READY = '[steps.ready-to-verify]\nenabled = true\nwho = "operator"\n'


def test_ready_to_verify_parks_until_an_allowed_reply(world):
    job = world.start(until="ready-to-verify", config_text=AUTO_MERGE + READY + verify(
        commands='["true"]', agent="true"))
    assert world.job(job)["running_since"] is None
    assert job not in [row["id"] for row in world.factory().store.running()]
    assert not (world.dir(job) / "verify-checks.md").exists()
    assert world.queue() == [("wait", 1, job, "ready-to-verify", "waiting for the operator (@operator)")]
    assert world.next(job)[0].startswith(f"wait {job} #1: ready-to-verify is waiting for the operator")

    world.comment(1, "/mfactory verify", "maintainer")
    assert world.next(job)[0].startswith("wait ")

    lines = world.answer(job, "verify")
    assert lines == [f"{job} #1: ready-to-verify VERIFY -> verify"]
    lines = world.next(job)
    assert lines[0] == "start-agent mfactory-verify"
    assert "Read ## Verify after deploy in spec.md" in lines[1]
    assert (world.dir(job) / "verify-checks.md").exists()
    world.verifier(job, "BLOCKED")
    assert "BLOCKED -> ready-to-verify" in world.done(job)[0]
    assert world.job(job)["step"] == "ready-to-verify"
    pr = str(world.job(job)["pr"])
    assert all("verification failed" not in comment["body"] for comment in world.gh["prs"][pr]["comments"])
    assert not any("revert" in " ".join(call) for call in world.calls("pr create"))

    world.answer(job, "verify")
    world.next(job)
    world.verifier(job, "PASS")
    assert world.done(job) == [f"{job} #1: verify PASS -> done"]


def test_a_verify_comment_from_the_operator_starts_the_check(world):
    job = world.start(until="ready-to-verify", config_text=AUTO_MERGE + READY + verify(commands='["true"]'))
    world.comment(1, "/mfactory verify", "operator")
    lines = world.next(job)
    assert lines[0] == f"{job} #1: ready-to-verify VERIFY -> verify"
    assert lines[1] == f"{job} #1: verify PASS -> done"


def test_blocked_four_times_escalates(world):
    job = world.start(until="ready-to-verify", config_text=AUTO_MERGE + READY + verify(
        commands='["true"]', agent="true"))
    for _ in range(3):
        world.answer(job, "verify")
        world.next(job)
        world.verifier(job, "BLOCKED")
        assert "BLOCKED -> ready-to-verify" in world.done(job)[0]
    world.answer(job, "verify")
    world.next(job)
    world.verifier(job, "BLOCKED")
    assert "max = 3 reached" in world.done(job)[0]
    assert world.job(job)["step"] == "escalated"


def test_blocked_fails_the_job_when_nobody_is_waiting(world):
    job = world.start(until="verify", config_text=AUTO_MERGE + verify(commands='["true"]', agent="true"))
    world.next(job)
    world.verifier(job, "BLOCKED")
    assert "verify FAIL -> failed" in world.done(job)[0]
    assert world.job(job)["step"] == "failed"
    pr = str(world.job(job)["pr"])
    assert "verification failed" in world.gh["prs"][pr]["comments"][-1]["body"]
    assert world.next(job)[0].startswith(f"finished {job} #1: failed")


def test_the_verifier_fail_outcome_still_fails_the_job(world):
    job = world.start(until="verify", config_text=AUTO_MERGE + verify(
        commands='["true"]', agent="true", retries=0))
    world.next(job)
    world.verifier(job, "FAIL")
    assert "verify FAIL -> failed" in world.done(job)[0]


def test_spec_must_say_what_to_check_after_deploy_when_verify_is_on(world):
    job = world.start(until="spec", config_text=verify(commands='["true"]'))
    prompt = world.next(job)[1]
    assert "## Verify after deploy" in prompt and "verify runs after merge" in prompt
    world.write(job, "spec.md", "STATUS: DONE\n\n# A thing\n\n## Acceptance criteria\n1. It exists.\n")
    with pytest.raises(Refused, match="Verify after deploy"):
        world.done(job)


def test_spec_can_omit_the_deploy_section_when_verify_is_off(world):
    job = world.start(until="spec")
    assert "Verify after deploy" not in world.next(job)[1]
    world.write(job, "spec.md", "STATUS: DONE\n\n# A thing\n\n## Acceptance criteria\n1. It exists.\n")
    assert world.done(job)[0].endswith("spec DONE -> build (skipped spec-approval)")


def test_verify_needs_something_to_check_and_a_wait_for_deploys(world):
    with pytest.raises(ConfigError) as error:
        config.load(world.repo, text='schema = 1\n[steps.verify]\nenabled = true\nafter = "deploy"\n')
    assert error.value.problems == [
        "[steps.verify] is on but checks nothing: set commands, or agent = true",
        '[steps.verify].wait is missing: with after = "deploy" it is the command that exits 0 once the merge '
        "commit is live"]
