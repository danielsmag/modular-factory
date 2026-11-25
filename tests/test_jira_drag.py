"""Answering a human step by dragging the job's card, and what happens to a card moved anywhere else."""

from conftest import JIRA_COLUMNS
from modfactory import board, config

SPEC_APPROVAL = """
[steps.spec-approval]
enabled = true
"""
# APPROVE passes through `debug`, which is switched off and continues to `build`.
THROUGH_A_DISABLED_STEP = """
[steps.spec-approval]
enabled = true
on = { APPROVE = "debug", CHANGES = { to = "spec", max = 3 }, REJECT = "rejected" }
"""
NOTE = "Factory: the card was moved back"


def parked(world, until, extra="", columns=JIRA_COLUMNS):
    """A job waiting at the human step `until`, and its card."""
    world.use_jira(extra=extra, columns=columns)
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, until)
    assert world.job(job)["step"] == until
    return job, key


def factory_moves(world, key):
    return [to for who, _, to in world.jira.moves(key) if who == "op"]


def notes(world, key):
    return [text for text in world.jira.comments(key) if NOTE in text]


# ---- Which column means which outcome: read from the graph ---------------------------


def statuses(world, extra=SPEC_APPROVAL, columns=JIRA_COLUMNS):
    world.use_jira(extra=extra, columns=columns)
    cfg = config.load(world.repo)
    table = cfg["tracker"]["jira"]["columns"]
    return cfg.steps, lambda step: board.status_for(table, step)


def test_a_column_is_an_outcome_when_one_edge_leads_to_its_step(world):
    steps, status_of = statuses(world)
    assert board.drag_outcomes(steps, status_of, "spec-approval", "Build") == ["APPROVE"]
    assert board.drag_outcomes(steps, status_of, "spec-approval", "Spec") == ["CHANGES"]
    assert board.drag_outcomes(steps, status_of, "spec-approval", "Rejected") == ["REJECT"]
    assert board.drag_outcomes(steps, status_of, "spec-approval", "Review") == []


def test_an_edge_through_a_disabled_step_is_dragged_to_where_the_job_lands(world):
    steps, status_of = statuses(world, extra=THROUGH_A_DISABLED_STEP)
    assert board.drag_outcomes(steps, status_of, "spec-approval", "Build") == ["APPROVE"]
    assert board.drag_outcomes(steps, status_of, "spec-approval", "Debug") == []


def test_two_outcomes_that_share_a_column_are_both_named(world):
    steps, status_of = statuses(world, columns=JIRA_COLUMNS.replace('spec = "Spec"', 'spec = "Build"'))
    assert board.drag_outcomes(steps, status_of, "spec-approval", "Build") == ["APPROVE", "CHANGES"]


def test_the_steps_own_column_is_no_outcome(world):
    shared = JIRA_COLUMNS.replace('spec-approval = "Spec approval"', 'spec-approval = "Build"')
    steps, status_of = statuses(world, columns=shared)
    assert board.drag_outcomes(steps, status_of, "spec-approval", "Build") == []


def test_every_draggable_column_of_a_step_is_listed(world):
    steps, status_of = statuses(world)
    assert board.drags(steps, status_of, "spec-approval") == {
        "Build": ["APPROVE"], "Spec": ["CHANGES"], "Rejected": ["REJECT"]}


# ---- A drag that answers ----------------------------------------------------------------


def test_dragging_the_card_to_an_outcomes_column_answers_the_step(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    before = factory_moves(world, key)
    world.jira.drag(key, "Build", "maint")
    assert world.next(job)[0] == f"{job} {key}: spec-approval APPROVE -> build"
    assert world.job(job)["step"] == "build"
    assert world.jira.status(key) == "Build"
    assert factory_moves(world, key) == before
    assert notes(world, key) == []


def test_the_operator_dragging_by_hand_is_a_person_answering(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.drag(key, "Build", "op")
    assert world.next(job)[0] == f"{job} {key}: spec-approval APPROVE -> build"


def test_a_drag_through_a_disabled_step_answers(world):
    job, key = parked(world, "spec-approval", THROUGH_A_DISABLED_STEP)
    world.jira.drag(key, "Build", "maint")
    assert world.next(job)[0] == f"{job} {key}: spec-approval APPROVE -> build (skipped debug)"


def test_dragging_to_rejected_rejects_without_words(world):
    job, key = parked(world, "ready-to-merge")
    world.jira.drag(key, "Rejected", "maint")
    world.next(job)
    assert world.job(job)["step"] == "rejected"
    rejection = (world.dir(job) / "rejection.md").read_text()
    assert "By Mia Maintainer at the ready-to-merge step." in rejection
    assert "Moved the card to `Rejected`" in rejection
    assert world.jira.status(key) == "Rejected"


def test_a_drag_takes_its_words_from_the_movers_comment(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.comment(key, "Tighten the acceptance criteria.", "maint")
    world.jira.drag(key, "Spec", "maint")
    assert world.next(job)[0] == f"{job} {key}: spec-approval CHANGES -> spec"
    feedback = (world.dir(job) / "feedback-1.md").read_text()
    assert "From Mia Maintainer at the spec-approval step (CHANGES)." in feedback
    assert "Tighten the acceptance criteria." in feedback
    assert len(world.job(job)["consumed"]) == 1


def test_another_persons_comment_is_not_the_movers_words(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.comment(key, "I would change everything.", "bob")
    world.jira.drag(key, "Spec", "maint")
    assert world.next(job)[0].startswith("wait ")
    assert world.jira.status(key) == "Spec approval"


def test_feedback_by_drag_needs_no_words_at_the_escalation_step(world):
    world.use_jira()
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, "build")
    world.next(job)
    world.build(job, status="BLOCKED", push=False, commit=False)
    world.done(job)
    assert world.job(job)["step"] == "escalated"
    assert world.jira.status(key) == "Escalated"
    world.jira.drag(key, "Build", "maint")
    assert world.next(job)[0] == f"{job} {key}: escalated FEEDBACK -> build"


def test_the_queue_reports_a_drag_as_a_reply_and_leaves_it_for_next(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.drag(key, "Build", "maint")
    assert world.queue() == [("resume", key, job, "spec-approval", "a reply is waiting")]
    assert world.job(job)["step"] == "spec-approval"
    assert world.next(job)[0] == f"{job} {key}: spec-approval APPROVE -> build"


def test_the_ask_says_which_columns_answer(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    ask = world.jira.comments(key)[-1]
    assert "or move this card: `Build` (APPROVE), `Spec` (CHANGES, after a comment saying what to change), " \
           "`Rejected` (REJECT)" in ask


# ---- A drag that does not answer is moved back ----------------------------------------------


def test_a_drag_that_needs_words_and_has_none_is_moved_back(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.drag(key, "Spec", "maint")
    assert world.next(job)[-1].startswith(f"wait {job} {key}: spec-approval is waiting for a maintainer")
    assert world.job(job)["step"] == "spec-approval"
    assert world.jira.status(key) == "Spec approval"
    [note] = notes(world, key)
    assert "Mia Maintainer moved this card to `Spec`" in note
    assert "CHANGES needs words" in note


def test_a_drag_to_a_column_that_is_no_answer_is_moved_back_with_the_columns_that_are(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.drag(key, "Review", "maint")
    world.next(job)
    assert world.jira.status(key) == "Spec approval"
    [note] = notes(world, key)
    assert "`Review` is not an answer to `spec-approval`" in note
    assert "`Build` (APPROVE)" in note
    assert "`Rejected` (REJECT)" in note


def test_a_column_two_outcomes_share_cannot_be_dragged_to(world):
    shared = JIRA_COLUMNS.replace('spec = "Spec"', 'spec = "Build"')
    job, key = parked(world, "spec-approval", SPEC_APPROVAL, columns=shared)
    world.jira.drag(key, "Build", "maint")
    world.next(job)
    assert world.job(job)["step"] == "spec-approval"
    assert world.jira.status(key) == "Spec approval"
    [note] = notes(world, key)
    assert "`Build` could mean APPROVE or CHANGES" in note
    assert "/mfactory approve" in note


def test_a_drag_by_someone_who_may_not_answer_is_moved_back(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.drag(key, "Build", "bob")
    world.next(job)
    assert world.job(job)["step"] == "spec-approval"
    assert world.jira.status(key) == "Spec approval"
    [note] = notes(world, key)
    assert "Bob Bystander moved this card to `Build`" in note
    assert "Only a maintainer may answer" in note


def test_a_note_about_a_wrong_drag_does_not_hide_the_comment_before_it(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.comment(key, "Split the second criterion.", "maint")
    world.jira.drag(key, "Review", "maint")
    world.next(job)
    assert len(notes(world, key)) == 1
    world.jira.drag(key, "Spec", "maint")
    assert world.next(job)[0] == f"{job} {key}: spec-approval CHANGES -> spec"
    assert "Split the second criterion." in (world.dir(job) / "feedback-1.md").read_text()


def test_a_wrong_drag_is_explained_once(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.drag(key, "Review", "maint")
    world.next(job)
    world.next(job)
    world.queue()
    assert len(notes(world, key)) == 1


def test_a_card_dragged_while_an_agent_works_is_moved_back_by_the_queue(world):
    job, key = parked(world, "build")
    world.jira.drag(key, "Review", "maint")
    rows = world.queue()
    assert rows == [("resume", key, job, "build", "round 1")]
    assert world.jira.status(key) == "Build"
    [note] = notes(world, key)
    assert "the factory moves this card while job" in note
    assert f"mfactory move {job}" in note
    assert world.job(job)["step"] == "build"


def test_a_card_jira_will_not_move_back_is_reported_without_a_comment(world):
    job, key = parked(world, "spec-approval", SPEC_APPROVAL)
    world.jira.drag(key, "Review", "maint")
    world.jira.workflow = {}
    said = world.next(job)
    assert any(line.startswith("note: ") and "`Review` to `Spec approval`" in line for line in said)
    assert notes(world, key) == []
    assert world.jira.status(key) == "Review"


# ---- A changed mapping --------------------------------------------------------------------


def test_a_card_follows_a_changed_mapping_without_a_comment(world):
    job, key = parked(world, "ready-to-merge")
    world.use_jira(columns=JIRA_COLUMNS.replace('ready-to-merge = "Ready to merge"', 'ready-to-merge = "Review"'))
    comments = len(world.jira.comments(key))
    assert world.next(job)[-1].startswith("wait ")
    assert world.jira.status(key) == "Review"
    assert len(world.jira.comments(key)) == comments


def test_the_queue_realigns_every_unfinished_job_after_a_mapping_change(world):
    job, key = parked(world, "build")
    world.use_jira(columns=JIRA_COLUMNS.replace('build = "Build"', 'build = "Review"'))
    world.queue()
    assert world.jira.status(key) == "Review"
    assert notes(world, key) == []


def test_reading_the_board_takes_one_call_for_all_jobs(world):
    world.use_jira()
    keys = [world.jira.ticket(f"Card {n}") for n in range(3)]
    for key in keys:
        world.play(world.create(key), "spec")
    world.jira.calls.clear()
    world.queue()
    assert len(world.jira.called("GET", "/board/7/issue")) == 2  # the queue column, and the jobs' cards
    assert world.jira.called("GET", "/rest/api/3/issue/") == []


def test_a_github_project_reads_no_board(world):
    job = world.start(until="ready-to-merge")
    assert world.job(job).get("board") is None
    assert world.queue() == [("wait", 1, job, "ready-to-merge", "waiting for a maintainer")]
