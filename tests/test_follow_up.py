"""After the merge: a post-debug step finds a problem, and the same job opens a follow-up fix pull request."""

import subprocess
from pathlib import Path

import pytest
from conftest import BASE_CHECKS, BASE_CONFIG
from modfactory import config
from modfactory.util import ConfigError, Refused

FOLLOW_UP = """
[steps.approve]
on = { APPROVE = "merge" }

[steps.ready-to-merge]
enabled = false
skip = "REJECT"

[steps.merge]
enabled = true
on = { MERGED = "after-merge" }

[steps.after-merge]
type = "human"
who = "maintainer"
ask = "merged: close the job, or debug"
on = { CLOSE = "done", DEBUG = "post-debug", ESCALATE = "escalated" }

[steps.post-debug]
type = "agent"
on = { FOUND = { to = "build", max = 1 }, UNKNOWN = "after-merge" }
"""


def post_debug(world, job, outcome="FOUND"):
    j = world.job(job)
    world.write(job, f"round-{j['round']}/post-debug.md",
                f"DIAGNOSIS: {outcome}\n\n## Cause\nTests run with -n 4.\n\n## Fix\nRun them serially.\n")


def merged_job(world):
    job = world.start(until="after-merge", config_text=FOLLOW_UP)
    assert world.job(job)["merge_sha"]
    return job


def test_debug_after_the_merge_starts_the_post_merge_debugger_on_a_checkout_of_the_base(world):
    job = merged_job(world)
    j = world.job(job)
    world.answer(job, "DEBUG", "CI got twice as slow")
    lines = world.next(job)
    checkout = f"{world.repo}/.worktrees/{job}-investigate"
    assert lines[0] == "start-agent mfactory-post-debug"
    assert f"PR: #{j['pr']}. Merge commit: {j['merge_sha']}." in lines[1]
    assert f"Checkout of origin/main: {checkout}." in lines[1]
    assert f"Worktree: {checkout}." in lines[1]
    assert lines[1].split("Write: ")[1].startswith(f"{world.dir(job)}/round-1/post-debug.md.")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True).stdout
    assert head.strip() == subprocess.run(["git", "rev-parse", "origin/main"], cwd=world.repo,
                                          capture_output=True, text=True).stdout.strip()


def test_the_post_merge_debugger_may_not_change_its_checkout(world):
    job = merged_job(world)
    world.answer(job, "DEBUG", "CI got slower")
    world.next(job)
    (Path(f"{world.repo}/.worktrees/{job}-investigate") / "stray.txt").write_text("x\n")
    post_debug(world, job)
    with pytest.raises(Refused, match="post-debug agent left changes"):
        world.done(job)


def test_found_starts_a_follow_up_build_on_a_new_branch_from_the_base(world):
    job = merged_job(world)
    first = world.job(job)
    world.answer(job, "DEBUG", "CI got slower")
    world.next(job)
    post_debug(world, job)
    assert world.done(job) == [f"{job} #1: post-debug FOUND -> build"]
    j = world.job(job)
    assert j["branch"] == f"factory/{job}-fix-1" and j["follow_ups"] == 1
    assert j["pr"] is None and j["merge_sha"] is None
    assert j["prs"] == [{"number": first["pr"], "url": first["pr_url"], "branch": f"factory/{job}",
                         "merge_sha": first["merge_sha"], "round": 1}]
    assert j["round"] == 2 and not Path(f"{world.repo}/.worktrees/{job}-investigate").exists()
    tree = j["worktree"]
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=tree, capture_output=True, text=True).stdout
    assert branch.strip() == f"factory/{job}-fix-1"
    prompt = world.next(job)[1]
    assert "Build round 2, attempt 1." in prompt
    assert "The post-merge diagnosis is in round-1/post-debug.md." in prompt
    assert "replied DEBUG at the after-merge step: read feedback-1.md" in prompt
    assert (f"Follow-up 1: pull request #{first['pr']} merged as {first['merge_sha'][:7]}." in prompt)
    assert f"open a new one, whose body starts with `Refs #{j['issue']}`" in prompt


def test_the_follow_up_merges_as_a_second_pull_request_of_the_same_job(world):
    job = merged_job(world)
    first = world.job(job)["pr"]
    world.answer(job, "DEBUG", "CI got slower")
    world.next(job)
    post_debug(world, job)
    world.done(job)
    world.play(job, until="after-merge")
    j = world.job(job)
    assert j["pr"] != first and j["merge_sha"] and [p["number"] for p in j["prs"]] == [first]
    assert world.gh["prs"][str(j["pr"])]["headRefName"] == f"factory/{job}-fix-1"
    assert world.gh["prs"][str(j["pr"])]["state"] == "MERGED"
    assert len(world.calls("pr merge")) == 2
    world.answer(job, "CLOSE")
    assert world.job(job)["step"] == "done"


def test_the_found_edge_max_limits_follow_ups(world):
    job = merged_job(world)
    world.answer(job, "DEBUG", "CI got slower")
    world.next(job)
    post_debug(world, job)
    world.done(job)
    world.play(job, until="after-merge")
    world.answer(job, "DEBUG", "still slow")
    world.next(job)
    post_debug(world, job)
    world.done(job)
    j = world.job(job)
    assert j["step"] == "escalated" and j["reason"] == "[steps.post-debug].on.FOUND max = 1 reached"
    assert j["follow_ups"] == 1


def test_feedback_after_the_merge_also_builds_a_follow_up(world):
    job = merged_job(world)
    world.answer(job, "ESCALATE", "needs a person")
    world.answer(job, "FEEDBACK", "Run the tests serially")
    j = world.job(job)
    assert j["step"] == "build" and j["branch"] == f"factory/{job}-fix-1" and j["pr"] is None


def test_found_with_a_closed_issue_is_refused(world):
    job = merged_job(world)
    world.answer(job, "DEBUG", "CI got slower")
    world.next(job)
    world.set_gh(lambda state: state["issues"]["1"].update(state="CLOSED"))
    post_debug(world, job)
    with pytest.raises(Refused, match="issue #1 is closed"):
        world.done(job)
    assert world.job(job)["step"] == "post-debug"


def test_unknown_goes_back_to_the_person(world):
    job = merged_job(world)
    world.answer(job, "DEBUG", "CI got slower")
    world.next(job)
    post_debug(world, job, "UNKNOWN")
    assert world.done(job) == [f"{job} #1: post-debug UNKNOWN -> after-merge (waiting for a maintainer)"]
    assert world.job(job)["follow_ups"] == 0


# ---- config ----------------------------------------------------------------


def test_a_post_debug_step_needs_both_outcomes(world):
    world.configure(FOLLOW_UP.replace(', UNKNOWN = "after-merge"', ""))
    with pytest.raises(ConfigError, match=r"\[steps.post-debug\].on has no edge for UNKNOWN"):
        config.load(world.repo)


def test_a_branch_pattern_must_accept_fix_branches_when_post_debug_is_on(world):
    pattern = 'branch_pattern = "factory/(revert-)?[0-9]{3}-[a-z-]+"\n'
    (world.repo / "factory.toml").write_text(BASE_CONFIG + pattern + BASE_CHECKS)
    config.load(world.repo)
    (world.repo / "factory.toml").write_text(BASE_CONFIG + pattern + BASE_CHECKS + FOLLOW_UP)
    with pytest.raises(ConfigError, match="does not match 'factory/000-add-dark-mode-fix-1'"):
        config.load(world.repo)


def test_the_readme_recipe_is_a_valid_config(world):
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    recipe = readme.split("### After the merge: close, test or debug", 1)[1].split("```toml\n", 1)[1].split("```", 1)[0]
    assert recipe.startswith("[project]\n")  # its keys join the base config's [project] table
    (world.repo / "factory.toml").write_text(BASE_CONFIG + recipe.removeprefix("[project]\n") + BASE_CHECKS)
    cfg = config.load(world.repo)
    assert cfg.role("post-debug") == "post-debug"
    assert cfg.steps["merge"]["on"]["MERGED"]["to"] == "after-merge"
