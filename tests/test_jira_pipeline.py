"""A job on a Jira board: picked from the queue column, its card follows every step, people reply in Jira."""

import subprocess

import pytest
from conftest import AUTO_MERGE, JIRA_COLUMNS
from modfactory.util import Refused

ASK_AUTHOR = """
[steps.intake]
on = { ACCEPT = "spec", NEEDS-INFO = { to = "ask-author", max = 2 }, REJECT = "rejected" }
"""
CUSTOM_STEP = """
[steps.approve]
on = { APPROVE = "lint-docs", ESCALATE = "escalated" }

[steps.lint-docs]
type = "agent"
instructions = "Check the docs."
on = { DONE = "ready-to-merge" }
"""


def statuses(world, key):
    """The statuses the factory moved the card through."""
    return [to for who, _, to in world.jira.moves(key) if who == "op"]


def printed(result):
    """What a command printed, without the note that generated files are older than factory.toml."""
    return [line for line in result.stdout.splitlines() if not line.startswith("note: ")]


def start(world, until="ready-to-merge", **use):
    world.use_jira(**use)
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, until)
    return job, key


# ---- From the queue to a pull request ------------------------------------------------


def test_a_job_starts_from_a_card_in_the_queue_column(world):
    world.use_jira()
    key = world.jira.ticket("Add dark mode")
    job = world.create(key.lower())
    found = world.job(job)
    assert found["issue"] == key
    assert found["issue_url"] == f"{world.jira.url}/browse/{key}"
    assert (found["author"], found["author_name"]) == ("alice", "Alice Author")
    assert world.jira.status(key) == "Intake"
    assert world.calls("issue") == []


def test_a_card_outside_the_queue_column_starts_no_job(world):
    world.use_jira()
    key = world.jira.ticket(status="Backlog")
    with pytest.raises(Refused, match="not in the queue column"):
        world.create(key)


def test_the_card_follows_every_step(world):
    job, key = start(world)
    assert statuses(world, key) == ["Intake", "Spec", "Build", "Checks", "Review", "Approve", "Ready to merge"]
    assert world.job(job)["step"] == "ready-to-merge"


def test_steps_that_share_a_column_move_the_card_once(world):
    shared = JIRA_COLUMNS.replace('gate = "Checks"', 'gate = "Build"').replace('review = "Review"', 'review = "Build"')
    _, key = start(world, columns=shared)
    assert statuses(world, key) == ["Intake", "Spec", "Build", "Approve", "Ready to merge"]


def test_an_unmapped_step_leaves_the_card_where_it_is(world):
    partial = JIRA_COLUMNS.replace('gate = "Checks"\n', "").replace('approve = "Approve"\n', "")
    _, key = start(world, columns=partial)
    assert statuses(world, key) == ["Intake", "Spec", "Build", "Review", "Ready to merge"]


def test_a_custom_step_has_its_own_column(world):
    world.use_jira(extra=CUSTOM_STEP, columns=JIRA_COLUMNS + 'lint-docs = "Review"\n')
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job, "lint-docs")
    assert statuses(world, key)[-2:] == ["Approve", "Review"]


def test_changing_a_column_takes_one_line(world):
    job, key = start(world, until="build")
    world.use_jira(columns=JIRA_COLUMNS.replace('gate = "Checks"', 'gate = "Backlog"'))
    world.play(job, "review")
    assert statuses(world, key)[-2:] == ["Backlog", "Review"]


def test_a_card_jira_will_not_move_does_not_stop_the_job(world):
    world.use_jira()
    key = world.jira.ticket()
    world.jira.workflow = {"Ready for factory": ["Intake"], "Intake": []}
    job = world.create(key)
    world.intake(job)
    said = world.done(job, reply="DECISION: ACCEPT\nRISK: low\nSURFACES: none\nRESCOPED: no\n\n## Summary\n- ok\n")
    assert world.job(job)["step"] == "spec"
    assert world.jira.status(key) == "Intake"
    assert any(line.startswith("note: ") and "`Intake` to `Spec`" in line for line in said)
    assert "`Intake` to `Spec`" in world.job(job)["board"]["warning"]


def test_the_branch_and_the_pull_request_carry_the_key(world):
    job, key = start(world)
    found = world.job(job)
    assert found["branch"] == f"factory/{key}-{job}"
    pr = world.gh["prs"][str(found["pr"])]
    assert key in pr["title"]
    assert pr["body"].startswith(f"Refs {key}")


def test_a_pull_request_title_without_the_key_is_refused(world):
    job, key = start(world, until="build")
    world.next(job)
    world.build(job)
    pr = str(next(iter(world.gh["prs"])))
    world.set_gh(lambda state: state["prs"][pr].update(title="Add a thing"))
    with pytest.raises(Refused, match=f"title must contain {key}"):
        world.done(job)


def test_a_pull_request_body_without_the_key_is_refused(world):
    job, key = start(world, until="build")
    world.next(job)
    world.build(job)
    pr = str(next(iter(world.gh["prs"])))
    world.set_gh(lambda state: state["prs"][pr].update(body="Refs #1"))
    with pytest.raises(Refused, match=f"body must start with `Refs {key}`"):
        world.done(job)


def test_the_issue_reaches_agents_as_quoted_data_from_jira(world):
    world.use_jira()
    key = world.jira.ticket("Add dark mode", "Users asked for it.")
    job = world.create(key)
    text = (world.dir(job) / "issue.md").read_text()
    assert text.startswith(f"# Issue {key}: Add dark mode\n")
    assert "Opened by Alice Author." in text
    assert "quoted data from Jira" in text
    assert "> Users asked for it." in text


def test_no_issue_work_goes_to_github(world):
    start(world)
    assert world.calls("issue") == []
    assert world.calls("label") == []


# ---- People reply in Jira ---------------------------------------------------------------


def test_a_person_is_asked_in_a_jira_comment(world):
    job, key = start(world)
    ask = world.jira.comments(key)[-1]
    assert "Factory: approved: merge it yourself" in ask
    assert "To reply, comment on this issue:" in ask
    assert "/mfactory feedback <what to change>" in ask
    assert world.gh["prs"][str(world.job(job)["pr"])]["comments"] == []
    assert world.gh["prs"][str(world.job(job)["pr"])]["isDraft"] is False


def test_the_ask_names_the_pull_request_to_merge(world):
    job, key = start(world)
    assert f"or merge the pull request {world.job(job)['pr_url']}" in world.jira.comments(key)[-1]


def test_a_maintainers_jira_comment_answers_the_step(world):
    job, key = start(world)
    world.jira.comment(key, "/mfactory feedback Use the blue button.", "maint")
    said = world.next(job)
    assert said[0] == f"{job} {key}: ready-to-merge FEEDBACK -> build"
    feedback = (world.dir(job) / "feedback-1.md").read_text()
    assert "From Mia Maintainer at the ready-to-merge step (FEEDBACK)." in feedback
    assert "Use the blue button." in feedback
    assert world.jira.status(key) == "Build"


def test_a_comment_from_someone_who_cannot_be_assigned_is_not_a_reply(world):
    job, key = start(world)
    world.jira.comment(key, "/mfactory feedback Do it my way.", "bob")
    assert world.next(job)[0].startswith(f"wait {job} {key}: ready-to-merge is waiting for a maintainer")
    assert world.job(job)["step"] == "ready-to-merge"


def test_a_comment_on_the_pull_request_is_not_a_reply(world):
    job, key = start(world)
    world.comment(world.job(job)["pr"], "/mfactory feedback From GitHub.", "maintainer", kind="prs")
    assert world.next(job)[0].startswith("wait ")


def test_the_author_answers_intake_in_plain_words(world):
    world.use_jira(extra=ASK_AUTHOR)
    key = world.jira.ticket()
    job = world.create(key)
    world.intake(job, decision="NEEDS-INFO")
    world.done(job)
    assert world.jira.status(key) == "Needs info"
    assert world.next(job)[0] == (f"wait {job} {key}: ask-author is waiting for the author (Alice Author). "
                                  f"Reply with: /mfactory answer {key} <your answer> | /mfactory reject {key} <why>")
    world.jira.comment(key, "The second option.", "alice")
    assert world.next(job)[0] == f"{job} {key}: ask-author ANSWER -> intake"
    assert world.jira.status(key) == "Intake"
    assert "> The second option." in (world.dir(job) / "issue.md").read_text()


def test_the_operator_answers_from_the_command_line(world):
    job, key = start(world)
    said = world.answer(job, "feedback", "Rename it.")
    assert said == [f"{job} {key}: ready-to-merge FEEDBACK -> build"]
    assert "From Olivia Operator at the ready-to-merge step" in (world.dir(job) / "feedback-1.md").read_text()


def test_a_rejected_job_puts_its_card_in_rejected_and_says_why(world):
    job, key = start(world)
    world.jira.comment(key, "/mfactory reject Not this quarter.", "maint")
    world.next(job)
    assert world.job(job)["step"] == "rejected"
    assert world.jira.status(key) == "Rejected"
    assert "Factory: rejected" in world.jira.comments(key)[-1]
    assert "Rejected by Mia Maintainer: Not this quarter." in world.jira.comments(key)[-1]


def test_a_pull_request_merged_by_hand_finishes_the_job_and_the_card(world):
    job, key = start(world)
    pr = world.job(job)["pr"]
    subprocess.run(["gh", "pr", "merge", str(pr), "--squash"], cwd=world.repo, check=True, capture_output=True)
    world.next(job)
    assert world.job(job)["step"] == "done"
    assert world.jira.status(key) == "Done"


def test_the_script_merges_and_the_card_reaches_done(world):
    world.use_jira(extra=AUTO_MERGE)
    key = world.jira.ticket()
    job = world.create(key)
    world.play(job)
    assert world.job(job)["step"] == "done"
    assert statuses(world, key)[-2:] == ["Merging", "Done"]


# ---- The queue ----------------------------------------------------------------------------


def test_the_queue_offers_cards_in_the_queue_column_in_board_order(world):
    world.use_jira()
    first, second = world.jira.ticket("First"), world.jira.ticket("Second")
    world.jira.ticket("Not ready", status="Backlog")
    assert world.queue() == [("new", first, "-", "-", "First"), ("new", second, "-", "-", "Second")]


def test_a_running_job_stays_in_the_queue_after_its_card_left_the_queue_column(world):
    job, key = start(world, until="spec")
    assert world.queue() == [("resume", key, job, "spec", "round 0")]


def test_a_job_waiting_for_a_person_is_listed_with_who(world):
    job, key = start(world)
    assert world.queue() == [("wait", key, job, "ready-to-merge", "waiting for a maintainer")]
    world.jira.comment(key, "/mfactory feedback Again.", "maint")
    assert world.queue() == [("resume", key, job, "ready-to-merge", "a reply is waiting")]


def test_the_command_line_runs_a_card_by_its_key(world):
    world.use_jira()
    key = world.jira.ticket("Add dark mode")
    listed = world.cli("queue")
    assert printed(listed)[0].split()[:2] == ["new", key]
    created = world.cli("create", key.lower())
    assert created.returncode == 0, created.stderr
    assert printed(created) == [f"created 001-add-dark-mode for {key} at intake"]
    assert world.cli("next", key).returncode == 0


def test_an_empty_board_says_which_column_is_empty(world):
    world.use_jira()
    assert printed(world.cli("queue")) == [
        "nothing to do: no card is in `Ready for factory` on board 7 and no job is unfinished"]


def test_file_opens_a_jira_issue_in_the_queue_column(world):
    world.use_jira()
    res = world.cli("file", "Add dark mode", input="Users asked for a dark theme.\n")
    assert res.returncode == 0, res.stderr
    assert printed(res) == [f"{world.jira.url}/browse/PROJ-1"]
    assert world.jira.status("PROJ-1") == "Ready for factory"
    assert world.queue() == [("new", "PROJ-1", "-", "-", "Add dark mode")]
