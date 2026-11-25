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
