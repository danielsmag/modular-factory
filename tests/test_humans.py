"""Human steps: asking, waiting, and replies from the CLI and from GitHub comments."""

import pytest
from modfactory.util import MARKER, Refused

SPEC_APPROVAL = "[steps.spec-approval]\nenabled = true\n"


def test_spec_approval_parks_the_job_and_shows_the_spec(world):
    job = world.start(until="spec-approval", config_text=SPEC_APPROVAL)
    j = world.job(job)
    assert j["waiting"]["who"] == "maintainer" and j["running_since"] is None
    asked = world.comments(1)[-1]
    assert asked.startswith(f"{MARKER}\n### Factory: approve the spec before it is built")
    assert "**spec.md**" in asked and "1. It exists." in asked
    assert "- `/factory approve`" in asked and "- `/factory changes <what to change>`" in asked
    assert "factory:needs-human" in world.labels(1)
    assert world.next(job) == [
        f"wait {job} #1: spec-approval is waiting for a maintainer. Reply with: "
        "/factory approve 1 | /factory changes 1 <what to change> | /factory reject 1 <why>"]


def test_approve_from_the_cli_continues_and_removes_the_label(world):
    job = world.start(until="spec-approval", config_text=SPEC_APPROVAL)
    assert world.answer(job, "approve") == [f"{job} #1: spec-approval APPROVE -> build"]
    assert "factory:needs-human" not in world.labels(1)
    world.play(job)
    assert world.job(job)["step"] == "done"


def test_changes_need_words_and_reach_the_spec_writer(world):
    job = world.start(until="spec-approval", config_text=SPEC_APPROVAL)
    with pytest.raises(Refused, match="CHANGES needs words"):
        world.answer(job, "changes")
    world.answer(job, "changes", "Leave out the export.")
    assert (world.dir(job) / "feedback-1.md").read_text().endswith("Leave out the export.\n")
    prompt = world.next(job)[1]
    assert "@operator replied CHANGES at the spec-approval step: read feedback-1.md" in prompt


def test_an_outcome_the_step_lacks_is_refused(world):
    job = world.start(until="spec-approval", config_text=SPEC_APPROVAL)
    with pytest.raises(Refused, match=r"spec-approval takes: APPROVE, CHANGES, REJECT \(not FEEDBACK\)"):
        world.answer(job, "feedback", "x")
    job2 = world.start(until="spec", title="Other")
    with pytest.raises(Refused, match="isn't waiting for a person"):
        world.answer(job2, "approve")


def test_a_maintainer_can_approve_with_a_comment(world):
    job = world.start(until="spec-approval", config_text=SPEC_APPROVAL)
    world.comment(1, "/factory approve", "maintainer")
    assert world.queue()[0] == ("resume", 1, job, "spec-approval", "a reply is waiting")
    assert world.next(job)[0] == f"{job} #1: spec-approval APPROVE -> build"


def test_comments_from_people_without_write_access_are_ignored(world):
    job = world.start(until="spec-approval", config_text=SPEC_APPROVAL)
    world.comment(1, "/factory approve", "alice")       # the author, but not a maintainer
    world.comment(1, "/factory approve", "stranger")
    assert world.next(job)[0].startswith("wait ")


def test_a_comment_from_before_the_question_does_not_answer_it(world):
    world.configure(SPEC_APPROVAL)
    number = world.issue()
    world.comment(number, "/factory approve", "maintainer")
    job = world.create(number)
    world.play(job, until="spec-approval")
    assert world.next(job)[0].startswith("wait ")


def test_changes_by_comment_carry_the_words_and_are_used_once(world):
    job = world.start(until="spec-approval", config_text=SPEC_APPROVAL)
    world.comment(1, "/factory changes #1 Make it smaller.\nAnd faster.", "maintainer")
    assert world.next(job)[0] == f"{job} #1: spec-approval CHANGES -> spec"
    assert "Make it smaller.\nAnd faster." in (world.dir(job) / "feedback-1.md").read_text()
    world.spec(job)
    world.done(job)
    assert world.next(job)[0].startswith("wait ")       # the same comment doesn't answer again


def test_reject_closes_the_job_and_posts_the_reason(world):
    job = world.start(until="spec-approval", config_text=SPEC_APPROVAL)
    assert world.answer(job, "reject", "Not now.") == [f"{job} #1: spec-approval REJECT -> rejected"]
    assert world.comments(1)[-1] == f"{MARKER}\n### Factory: rejected\n\nRejected by @operator: Not now."
    assert (world.dir(job) / "rejection.md").exists()
    assert world.labels(1) == []
    assert not (world.repo / ".worktrees" / job).exists()


def test_needs_info_asks_the_author_and_their_plain_answer_resumes_intake(world):
    number = world.issue()
    job = world.create(number)
    world.next(job)
    world.intake(job, decision="NEEDS-INFO")
    assert world.done(job) == [f"{job} #{number}: intake NEEDS-INFO -> ask-author (waiting for the author (@alice))"]
    asked = world.comments(number)[-1]
    assert "### Factory: intake has questions for the author" in asked and "1. Which?" in asked
    assert "- or answer in plain words" in asked
    assert "factory:needs-info" in world.labels(number)

    world.comment(number, "The second one.", "bob")      # not the author
    assert world.next(job)[0].startswith("wait ")
    world.comment(number, "The second one.", "alice")
    lines = world.next(job)
    assert lines[0] == f"{job} #{number}: ask-author ANSWER -> intake"
    assert lines[1] == "start-agent factory-intake"
    assert "@alice answered the questions" in lines[2]
    assert (world.dir(job) / "intake-1.md").exists() and not (world.dir(job) / "intake.md").exists()
    assert "> The second one." in (world.dir(job) / "issue.md").read_text()
    assert "factory:needs-info" not in world.labels(number)


def test_the_operator_can_answer_for_the_author(world):
    number = world.issue()
    job = world.create(number)
    world.next(job)
    world.intake(job, decision="NEEDS-INFO")
    world.done(job)
    world.answer(job, "answer", "Use the first.")
    assert world.comments(number)[-1] == "Use the first."
    assert world.job(job)["step"] == "intake"


def test_feedback_on_an_escalated_job_starts_a_new_round(world):
    job = world.start(until="approve")
    world.next(job)
    world.approve(job, "ESCALATE")
    world.done(job)
    pr = world.job(job)["pr"]
    world.comment(pr, "/factory feedback Rename the flag.", "maintainer", kind="prs")
    assert world.next(job)[0] == f"{job} #1: escalated FEEDBACK -> build"
    j = world.job(job)
    assert (j["round"], j["attempt"], j["taken"]) == (2, 1, {"escalated.FEEDBACK": 1})
    assert "read feedback-1.md" in world.next(job)[1]


def test_merging_the_pull_request_by_hand_approves_the_job(world):
    job = world.start(until="approve")
    world.next(job)
    world.approve(job, "ESCALATE")
    world.done(job)
    pr = world.job(job)["pr"]
    world.set_gh(lambda s: s["prs"][str(pr)].update(state="MERGED", mergeCommit={"oid": "abc1234"}))
    lines = world.next(job)
    assert lines[0] == f"{job} #1: PR #{pr} was merged by hand -> done"
    j = world.job(job)
    assert j["step"] == "done" and j["merge_sha"] == "abc1234"
    assert j["history"][-1]["outcome"] == "MERGED-BY-HAND"
    assert "factory:needs-human" not in world.labels(1)


def test_a_blocked_build_before_any_spec_change_returns_to_the_builder(world):
    job = world.start(until="build")
    world.next(job)
    world.build(job, status="BLOCKED", push=False)
    assert world.done(job) == [f"{job} #1: build BLOCKED -> escalated (waiting for a maintainer)"]
    asked = world.comments(1)[-1]                         # no pull request yet, so the issue is asked
    assert "The builder could not finish this job" in asked and "Needs a decision." in asked
    world.answer(job, "feedback", "Use option A.")
    assert world.job(job)["step"] == "build"


def test_a_custom_human_step_is_only_configuration(world):
    job = world.start(until="release-signoff", config_text="""
[steps.approve]
on = { APPROVE = "release-signoff" }

[steps.release-signoff]
type = "human"
who = "maintainer"
ask = "sign off this release"
on = { APPROVE = "merge", REJECT = "rejected" }
""")
    assert "### Factory: sign off this release" in world.gh["prs"]["2"]["comments"][-1]["body"]
    world.answer(job, "approve")
    world.play(job)
    assert world.job(job)["step"] == "done"
