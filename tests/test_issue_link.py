"""`[project] issue_link`, and closing an issue or a job by hand."""

import subprocess

import pytest
from conftest import BASE_CHECKS, BASE_CONFIG
from modfactory import config, graph, sync
from modfactory.util import MARKER, ConfigError, Refused

VERIFY = '[steps.verify]\nenabled = true\ncommands = ["true"]\n'


def load(world, text=""):
    return config.load(world.repo, text="schema = 1\n" + text)


def build_agent(world, text=""):
    files, _ = sync.plan(load(world, text))
    return files[".claude/agents/mfactory-build.md"]


def set_pr_body(world, job, text):
    pr = str(world.job(job)["pr"] or next(iter(world.gh["prs"])))
    world.set_gh(lambda state: state["prs"][pr].update(body=text))


# ---- config and the builder's instructions ---------------------------------


def test_issue_link_takes_refs_or_closes(world):
    assert load(world)["project"]["issue_link"] == "refs"
    with pytest.raises(ConfigError) as error:
        load(world, '[project]\nissue_link = "fixes"\n')
    assert any("issue_link" in problem for problem in error.value.problems)


def test_the_builder_is_told_how_to_link_the_issue(world):
    default = build_agent(world)
    assert "## Pull request body" in default and "The first line is `Refs #<issue>`." in default
    assert "Closes #<issue>" not in default
    assert "The first line is `Closes #<issue>`." in build_agent(world, '[project]\nissue_link = "closes"\n')


def test_doctor_warning_only_for_closes_with_a_step_after_the_merge(world):
    assert graph.closed_issue_warning(load(world, VERIFY).steps, "refs") is None
    assert graph.closed_issue_warning(load(world).steps, "closes") is None
    assert graph.closed_issue_warning(load(world, VERIFY).steps, "closes") == (
        '[steps.verify] runs after the merge, but [project].issue_link = "closes" lets GitHub close '
        'the issue when the pull request merges, so verify sees a closed issue. Use issue_link = "refs".')


# ---- the pull request body -------------------------------------------------


def closes_mode(world):
    (world.repo / "factory.toml").write_text(BASE_CONFIG + 'issue_link = "closes"\n' + BASE_CHECKS)


def to_ready_build(world):
    job = world.start(until="build")
    world.next(job)
    world.build(job)
    return job


def test_a_ready_build_must_link_the_issue_with_refs(world):
    job = to_ready_build(world)
    set_pr_body(world, job, "Closes #1\n\n## Summary\n- built")
    with pytest.raises(Refused, match="body must start with `Refs #1`"):
        world.done(job)
    set_pr_body(world, job, "Refs #1\n\nThis fixes #1 for good.")
    with pytest.raises(Refused, match="closing keyword next to #1"):
        world.done(job)
    set_pr_body(world, job, "Refs #1\n\n## Summary\n- built, see #12")
    assert world.done(job) == [f"{job} #1: build READY -> gate"]


def test_closes_mode_does_not_check_the_body(world):
    closes_mode(world)
    job = to_ready_build(world)
    set_pr_body(world, job, "Closes #1")
    assert world.done(job) == [f"{job} #1: build READY -> gate"]


def test_the_factory_never_closes_the_issue_itself(world):
    job = world.start(until="ready-to-merge", config_text=VERIFY)
    pr = str(world.job(job)["pr"])
    subprocess.run(["gh", "pr", "merge", pr, "--squash"], cwd=world.repo, check=True)
    lines = world.next(job)
    assert f"{job} #1: verify PASS -> done" in lines
    assert world.gh["issues"]["1"]["state"] == "OPEN"
    assert world.calls("issue close") == []


# ---- close issue -----------------------------------------------------------


def test_close_issue_closes_it_and_leaves_the_job(world):
    job = world.start(until="ready-to-merge")
    world.factory().close_issue(job)
    assert world.said == ["closed #1 (completed)",
                     f"note: job {job} is still at ready-to-merge; `.factory/bin/mfactory close job {job}` ends it"]
    issue = world.gh["issues"]["1"]
    assert issue["state"] == "CLOSED" and issue["stateReason"] == "COMPLETED"
    assert world.comments(1)[-1] == f"{MARKER}\n### Factory: issue closed by @operator (completed)"
    assert world.job(job)["step"] == "ready-to-merge"

    world.factory().close_issue(job)
    assert world.said == ["#1 was already closed"]


def test_close_issue_not_planned_after_a_rejection(world):
    job = world.start(until="ready-to-merge")
    world.answer(job, "reject", "Not now.")
    assert world.gh["issues"]["1"]["state"] == "OPEN"
    world._factory.store.release()
    res = world.cli("close", "issue", job, "--not-planned")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "closed #1 (not planned)"
    assert world.gh["issues"]["1"]["stateReason"] == "NOT_PLANNED"


# ---- close job -------------------------------------------------------------


def test_close_job_ends_a_waiting_job_as_done_and_leaves_the_issue(world):
    job = world.start(until="ready-to-merge")
    j = world.job(job)
    assert "factory:needs-human" in world.labels(1)
    world._factory.store.release()
    res = world.cli("close", "job", job)
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == [
        f"{job} #1: ready-to-merge CLOSED-BY-HAND -> done",
        f"note: pull request #{j['pr']} is still open",
        f"note: issue #1 is not closed; `.factory/bin/mfactory close issue {job}` closes it"]
    closed = world.job(job)
    assert closed["step"] == "done" and closed["finished_at"]
    assert closed["history"][-1]["outcome"] == "CLOSED-BY-HAND"
    assert closed["history"][-1]["reason"] == "Closed by @operator"
    assert "factory:needs-human" not in world.labels(1)
    assert not (world.repo / ".worktrees" / job).exists()
    assert world.gh["issues"]["1"]["state"] == "OPEN"
    assert world.gh["prs"][str(j["pr"])]["state"] == "OPEN"

    with pytest.raises(Refused, match="is already finished"):
        world.factory().close_job(job)
