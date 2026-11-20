"""Edges that go back carry `max`; a used-up max sends the job to a maintainer."""

import pytest
from modfactory import config
from modfactory.util import ConfigError


def fail_gate(world, job):
    """Build something that fails the checks and let the gate run."""
    world.next(job)
    world.build(job, files={"FAIL": "x\n"})
    world.done(job)
    return world.next(job)


def test_gate_fail_limit_escalates(world):
    job = world.start(until="build", config_text="[steps.gate]\non = { FAIL = { max = 1 } }\n")
    assert fail_gate(world, job)[0] == f"{job} #1: gate FAIL -> build (skipped debug)"   # the first retry is allowed
    j = world.job(job)
    assert (j["step"], j["round"], j["attempt"]) == ("build", 1, 2)

    line = fail_gate(world, job)[0]
    j = world.job(job)
    assert j["step"] == "escalated"
    assert j["reason"] == "[steps.gate].on.FAIL max = 1 reached"
    assert line == f"{job} #1: gate FAIL -> escalated ([steps.gate].on.FAIL max = 1 reached; waiting for a maintainer)"
    assert j["escalated_from"] == "gate"


def test_a_failed_gate_keeps_the_round_and_tells_the_builder_where_to_look(world):
    job = world.start(until="build")
    fail_gate(world, job)
    prompt = world.next(job)[1]
    assert "Build round 1, attempt 2." in prompt
    assert "The checks failed (round 1, attempt 1): read round-1/checks.md and round-1/checks.log" in prompt
    world.build(job, files={"FAIL": None})
    world.done(job)
    assert world.next(job)[0] == f"{job} #1: gate PASS -> review"
    assert (world.dir(job) / "round-1" / "checks-1.md").read_text().startswith("GATE: FAIL")
    assert (world.dir(job) / "round-1" / "checks.md").read_text().startswith("GATE: PASS\n")


def test_a_pass_resets_the_count_so_max_means_failures_in_a_row(world):
    job = world.start(until="build", config_text="[steps.gate]\non = { FAIL = { max = 1 } }\n")
    fail_gate(world, job)
    world.next(job)
    world.build(job, files={"FAIL": None})
    world.done(job)
    world.next(job)                                       # gate passes
    assert "gate.FAIL" not in world.job(job)["taken"]
    world.review(job, changes=["code"])
    world.done(job)
    assert fail_gate(world, job)[0].endswith("gate FAIL -> build (skipped debug)")   # allowed again


def test_max_zero_never_retries(world):
    job = world.start(until="build", config_text="[steps.review]\non = { CHANGES = { max = 0 } }\n")
    world.play(job, until="review")
    world.next(job)
    world.review(job, changes=["code"])
    assert world.done(job)[0].startswith(f"{job} #1: review CHANGES -> escalated ([steps.review].on.CHANGES max = 0")


def test_review_rounds_are_limited(world):
    job = world.start(until="review")
    for round_number in (1, 2):
        world.next(job)
        world.review(job, changes=["code"])
        assert world.done(job) == [f"{job} #1: review CHANGES -> build"]
        world.play(job, until="review")
        assert world.job(job)["round"] == round_number + 1
    world.next(job)
    world.review(job, changes=["code"])
    world.done(job)
    j = world.job(job)
    assert j["step"] == "escalated" and j["reason"] == "[steps.review].on.CHANGES max = 2 reached"


def test_feedback_from_a_maintainer_gives_a_fresh_budget(world):
    job = world.start(until="build", config_text="[steps.gate]\non = { FAIL = { max = 0 } }\n")
    fail_gate(world, job)
    assert world.job(job)["step"] == "escalated"
    world.answer(job, "feedback", "Try again without the FAIL file.")
    j = world.job(job)
    assert j["step"] == "build" and j["taken"] == {"escalated.FEEDBACK": 1}
    assert (j["round"], j["attempt"]) == (2, 1)
    world.next(job)
    world.build(job, files={"FAIL": None, "fixed.txt": "x\n"})
    world.done(job)
    world.play(job)
    assert world.job(job)["step"] == "done"


def test_needs_info_is_limited_too(world):
    job = world.create(world.issue())
    for _ in range(2):
        world.next(job)
        world.intake(job, decision="NEEDS-INFO")
        world.done(job)
        world.answer(job, "answer", "Like this.")
    world.next(job)
    world.intake(job, decision="NEEDS-INFO")
    assert "[steps.intake].on.NEEDS-INFO max = 2 reached" in world.done(job)[0]
    assert world.job(job)["step"] == "escalated"
    # There is no spec to build from, so feedback sends the job back to intake.
    world.answer(job, "feedback", "Accept the small version.")
    assert world.job(job)["step"] == "intake"


# ---- The graph itself ------------------------------------------------------


def problems(world, text):
    with pytest.raises(ConfigError) as error:
        config.load(world.repo, text="schema = 1\n" + text)
    return error.value.problems


def test_edges_must_point_at_steps(world):
    assert problems(world, '[steps.build]\non = { READY = "gaet" }\n') == [
        '[steps.build].on.READY points at unknown step "gaet". Steps: intake, ask-author, spec, spec-approval, '
        "build, gate, debug, review, approve, merge, verify, escalated; end states: done, failed, rejected"]


def test_built_in_steps_need_their_outcomes_and_no_others(world):
    found = problems(world, '[steps.gate]\non = { MAYBE = "build" }\n')
    assert "[steps.gate].on.MAYBE is not an outcome of a checks step. Outcomes: FAIL, PASS" in found


def test_a_loop_needs_a_max_or_a_person(world):
    found = problems(world, """
[steps.gate]
on = { PASS = "lint" }

[steps.lint]
type = "agent"
instructions = "Lint."
on = { OK = "review", REDO = "build" }
""")
    assert found == ["loop build -> gate -> lint -> build has no edge with max and no human step, "
                     "so a job could go round it forever. Add max to one of its edges"]


def test_a_step_nothing_points_at_is_reported(world):
    found = problems(world, '[steps.lint]\ntype = "agent"\ninstructions = "Lint."\non = { OK = "gate" }\n')
    assert found == ['[steps.lint] can\'t be reached from [run].start = "intake". '
                     "Point an edge at it, or set enabled = false"]


def test_a_disabled_step_needs_skip(world):
    found = problems(world, '[steps.lint]\ntype = "agent"\nenabled = false\ninstructions = "x"\non = { OK = "gate" }\n')
    assert found == ["[steps.lint] is off, so [steps.lint].skip must name the outcome to continue with: OK"]


def test_escalate_to_must_be_a_human_step(world):
    assert problems(world, '[run]\nescalate_to = "build"\n') == [
        '[run].escalate_to = "build" must be an enabled human step']


def test_a_custom_agent_step_runs_between_two_others(world):
    job = world.start(until="docs", config_text="""
[steps.gate]
on = { PASS = "docs" }

[steps.docs]
type = "agent"
instructions = "Update the changelog."
writes = "docs.md"
on = { DONE = "review", SKIP = "review" }
""")
    lines = world.next(job)
    assert lines[0] == "start-agent mfactory-docs"
    assert "Outcomes: DONE | SKIP." in lines[1] and lines[1].endswith(f"Write: {world.dir(job)}/docs.md.")
    world.write(job, "docs.md", "STATUS: SKIP\n")
    assert world.done(job) == [f"{job} #1: docs SKIP -> review"]
    world.play(job)
    assert [h["step"] for h in world.job(job)["history"]] == [
        "intake", "spec", "build", "gate", "docs", "review", "approve", "merge"]


def test_removing_a_default_step_is_one_line(world):
    job = world.start(config_text='[steps.approve]\nenabled = false\nskip = "APPROVE"\n')
    j = world.job(job)
    assert j["step"] == "done"
    assert [h["step"] for h in j["history"]] == ["intake", "spec", "build", "gate", "review", "merge"]
    assert j["history"][4]["skipped"] == ["approve"]
