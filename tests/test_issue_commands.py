"""`mfactory issue ...`: a person's own issues on GitHub and a Jira board, without starting a job."""

import json

from modfactory.util import MARKER


def ok(result):
    assert result.returncode == 0, result.stderr
    return result.stdout


def refused(result):
    assert result.returncode == 1, result.stdout
    return result.stderr


def listed(world, *args):
    return json.loads(ok(world.cli("issue", "list", "--json", *args)))


# ---- create ----------------------------------------------------------------------------


def test_create_opens_a_github_issue_out_of_the_queue(world):
    out = ok(world.cli("issue", "create", "Test", "issue", input="Just a test."))
    number = out.splitlines()[0].rsplit("/", 1)[1]
    issue = world.gh["issues"][number]
    assert issue["title"] == "Test issue" and issue["body"] == "Just a test."
    assert issue["labels"] == []
    assert "not in the factory's queue" in out
    assert world.queue() == []


def test_create_with_queue_puts_it_in_the_queue(world):
    out = ok(world.cli("issue", "create", "--queue", "Build it", input="Please."))
    number = out.strip().rsplit("/", 1)[1]
    assert world.labels(number) == ["factory"]
    assert [row[0] for row in world.queue()] == ["new"]


def test_create_on_jira_leaves_the_card_in_backlog(world):
    jira = world.use_jira()
    out = ok(world.cli("issue", "create", "Test issue", input="Body"))
    key = out.splitlines()[0].rsplit("/", 1)[1]
    assert jira.status(key) == "Backlog"


def test_create_chooses_the_tracker_with_two(world):
    jira = world.use_both()
    out = ok(world.cli("issue", "create", "--tracker", "jira", "On Jira", input="x"))
    assert "/browse/PROJ-" in out
    assert len(jira.issues) == 1


# ---- list ------------------------------------------------------------------------------


def test_list_shows_every_open_github_issue_and_mine(world):
    world.issue("Queued")
    world.issue("Not queued", labels=())
    world.issue("Mine", labels=(), assignees=("operator",))
    titles = [row["title"] for row in listed(world)["issues"]]
    assert set(titles) == {"Queued", "Not queued", "Mine"}
    mine = listed(world, "--mine")["issues"]
    assert [row["title"] for row in mine] == ["Mine"]
    assert mine[0]["assignees"] == ["operator"] and mine[0]["status"] == "open"


def test_list_on_jira_skips_done_cards_and_names_the_job(world):
    jira = world.use_jira()
    queued = jira.ticket("Queued")
    jira.ticket("Backlog card", status="Backlog", assignee="op")
    jira.ticket("Finished", status="Done")
    job = world.create(queued)
    rows = {row["title"]: row for row in listed(world)["issues"]}
    assert set(rows) == {"Queued", "Backlog card"}
    assert rows["Queued"]["job"]["id"] == job
    assert rows["Backlog card"]["status"] == "Backlog"
    assert [row["title"] for row in listed(world, "--mine")["issues"]] == ["Backlog card"]


def test_list_with_two_trackers_shows_both_or_one(world):
    jira = world.use_both()
    world.issue("On GitHub", labels=())
    jira.ticket("On Jira", status="Backlog")
    assert {row["tracker"] for row in listed(world)["issues"]} == {"github", "jira"}
    assert [row["title"] for row in listed(world, "--tracker", "jira")["issues"]] == ["On Jira"]


def test_list_through_acli(world):
    jira = world.use_acli()
    jira.ticket("Backlog card", status="Backlog")
    assert [row["title"] for row in listed(world)["issues"]] == ["Backlog card"]


def test_list_prints_one_line_per_issue(world):
    world.issue("Readable", labels=())
    out = ok(world.cli("issue", "list"))
    assert "#1  open  unassigned  Readable  https://github.test/acme/app/issues/1" in out


# ---- show ------------------------------------------------------------------------------


def test_show_prints_the_text_comments_and_where_a_card_can_move(world):
    jira = world.use_jira()
    jira.workflow = {"Backlog": ["Ready for factory", "Done"]}
    key = jira.ticket("Look", body="The text.", status="Backlog")
    jira.comment(key, "A remark", "op")
    out = ok(world.cli("issue", "show", key))
    assert f"{key}: Look" in out and "The text." in out and "A remark" in out
    assert "can move to: Ready for factory, Done" in out
    found = json.loads(ok(world.cli("issue", "show", key, "--json")))
    assert found["moves_to"] == ["Ready for factory", "Done"]
    assert found["comments"][0]["body"].strip() == "A remark"


def test_show_a_github_issue(world):
    number = world.issue("Look", body="Text", labels=())
    world.comment(number, "Hello", "alice")
    found = json.loads(ok(world.cli("issue", "show", number, "--json")))
    assert found["title"] == "Look" and found["moves_to"] is None
    assert [c["body"] for c in found["comments"]] == ["Hello"]


# ---- comment ---------------------------------------------------------------------------


def test_comment_posts_the_persons_words_without_the_factory_marker(world):
    number = world.issue("Talk", labels=())
    ok(world.cli("issue", "comment", number, input="My words"))
    assert world.comments(number) == ["My words"]
    assert not world.comments(number)[0].startswith(MARKER)


def test_comment_takes_words_on_the_command_line_and_refuses_none(world):
    jira = world.use_jira()
    key = jira.ticket("Talk", status="Backlog")
    ok(world.cli("issue", "comment", key, "Looks", "good"))
    assert len(jira.comments(key)) == 1
    assert "stdin" in refused(world.cli("issue", "comment", key, input=""))


# ---- move ------------------------------------------------------------------------------


def test_move_a_jira_card_to_a_status(world):
    jira = world.use_jira()
    key = jira.ticket("Move me", status="Backlog")
    assert f"moved {key} to Ready for factory" in ok(world.cli("issue", "move", key, "Ready", "for", "factory"))
    assert jira.status(key) == "Ready for factory"
    assert "already in" in ok(world.cli("issue", "move", key, "Ready for factory"))


def test_move_refuses_a_status_the_workflow_does_not_allow_and_names_the_ones_it_does(world):
    jira = world.use_jira()
    jira.workflow = {"Backlog": ["Done"]}
    key = jira.ticket("Stuck", status="Backlog")
    said = refused(world.cli("issue", "move", key, "Review"))
    assert "it can move to: Done" in said
    assert jira.status(key) == "Backlog"


def test_move_refuses_a_card_with_a_running_job(world):
    jira = world.use_jira()
    key = jira.ticket("Running")
    job = world.create(key)
    said = refused(world.cli("issue", "move", key, "Done"))
    assert f"mfactory move {job} <step>" in said


def test_move_refuses_a_github_issue(world):
    number = world.issue("No board", labels=())
    assert "no status" in refused(world.cli("issue", "move", number, "Done"))


def test_move_through_acli(world):
    jira = world.use_acli()
    key = jira.ticket("Move me", status="Backlog")
    ok(world.cli("issue", "move", key, "Ready for factory"))
    assert jira.status(key) == "Ready for factory"


# ---- close -----------------------------------------------------------------------------


def test_close_an_issue_without_a_job(world):
    number = world.issue("Close me", labels=())
    assert f"closed #{number} (not planned)" in ok(world.cli("issue", "close", number, "--not-planned"))
    assert world.gh["issues"][str(number)]["state"] == "CLOSED"
    assert "already closed" in ok(world.cli("issue", "close", number))


def test_close_an_issue_with_a_job_goes_through_the_job(world):
    number = world.issue("Has a job")
    job = world.create(number)
    world.factory().store.release()  # the command line takes the job's lock itself
    out = ok(world.cli("issue", "close", number))
    assert world.gh["issues"][str(number)]["state"] == "CLOSED"
    assert job in out  # the note that the job is still running
