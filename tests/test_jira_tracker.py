"""The Jira tracker against a fake Jira: the queue column, issues, comments, people and card moves."""

import pytest
from conftest import JIRA_COLUMNS
from modfactory import config, install
from modfactory.service import tracker as trackers
from modfactory.util import Refused


def tracker(world, **use):
    world.use_jira(**use)
    return trackers.make(config.load(world.repo))


# ---- Keys ---------------------------------------------------------------------


def test_a_jira_key_is_its_issue_key_in_upper_case(world):
    found = tracker(world)
    assert found.kind == "jira"
    assert found.key("proj-12") == "PROJ-12"
    assert found.key(" PROJ-12 ") == "PROJ-12"
    assert found.ref("PROJ-12") == "PROJ-12"


def test_a_number_is_not_a_jira_key(world):
    found = tracker(world)
    for ref in ("12", "#12", "PROJ", "PROJ-1/../x"):
        with pytest.raises(Refused, match="PROJ-123"):
            found.key(ref)


# ---- The queue ------------------------------------------------------------------


def test_the_queue_is_the_boards_cards_in_the_queue_column(world):
    found = tracker(world)
    queued = world.jira.ticket("Queued")
    world.jira.ticket("Not ready", status="Backlog")
    world.jira.ticket("Under way", status="Build")
    world.jira.ticket("Off the board", status="Archived")
    issues = found.queue()
    assert [issue["number"] for issue in issues] == [queued]
    assert issues[0]["title"] == "Queued"
    assert issues[0]["url"] == f"{world.jira.url}/browse/{queued}"
    assert issues[0]["author"] == {"login": "alice", "name": "Alice Author"}


def test_the_queue_is_read_across_pages_in_the_boards_order(world):
    found = tracker(world)
    world.jira.page_size = 2
    keys = [world.jira.ticket(f"Card {n}") for n in range(5)]
    assert [issue["number"] for issue in found.queue()] == keys


def test_the_projects_jql_narrows_the_queue(world):
    found = tracker(world, settings='jql = "assignee = currentUser()"')
    world.jira.ticket("Someone else's")
    mine = world.jira.ticket("Mine", assignee="op")
    assert [issue["number"] for issue in found.queue()] == [mine]


def test_the_queue_may_be_several_statuses(world):
    found = tracker(world, columns=JIRA_COLUMNS.replace('queue = "Ready for factory"',
                                                        'queue = ["Ready for factory", "Backlog"]'))
    keys = [world.jira.ticket("Ready"), world.jira.ticket("In the backlog", status="Backlog")]
    assert [issue["number"] for issue in found.queue()] == keys


def test_a_card_in_the_queue_column_passes_the_gate(world):
    found = tracker(world)
    assert found.queue_gate(found.issue(world.jira.ticket())) is None


def test_a_card_in_another_column_does_not_pass_the_gate(world):
    found = tracker(world)
    key = world.jira.ticket(status="Backlog")
    gate = found.queue_gate(found.issue(key))
    assert f"{key} is in `Backlog`" in gate
    assert "`Ready for factory`" in gate


def test_a_queued_card_the_projects_jql_leaves_out_does_not_pass_the_gate(world):
    found = tracker(world, settings='jql = "assignee = currentUser()"')
    key = world.jira.ticket()
    assert "assignee = currentUser()" in found.queue_gate(found.issue(key))


# ---- Issues ---------------------------------------------------------------------


def test_an_issue_reads_as_plain_text(world):
    found = tracker(world)
    key = world.jira.ticket("Add dark mode", "Users asked.\n\nMake it the default.")
    issue = found.issue(key)
    assert issue["number"] == key
    assert issue["title"] == "Add dark mode"
    assert issue["body"] == "Users asked.\n\nMake it the default."
    assert issue["state"] == "OPEN"
    assert issue["url"] == f"{world.jira.url}/browse/{key}"
    assert issue["author"]["login"] == "alice"
    assert issue["comments"] == []


def test_an_issue_in_a_done_status_is_closed(world):
    found = tracker(world)
    assert found.issue(world.jira.ticket(status="Done"))["state"] == "CLOSED"


def test_an_issue_that_does_not_exist_is_refused_by_name(world):
    with pytest.raises(Refused, match="PROJ-99"):
        tracker(world).issue("PROJ-99")


def test_filing_an_issue_creates_it_in_the_boards_project(world):
    found = tracker(world)
    url = found.create_issue("Report a problem", "### What\n\nIt broke.")
    assert url == f"{world.jira.url}/browse/PROJ-1"
    created = world.jira.issues["PROJ-1"]
    assert (created["summary"], created["type"], created["status"]) == ("Report a problem", "Task", "Backlog")
    assert created["description"]["content"][0]["type"] == "heading"


def test_filing_an_issue_for_the_queue_puts_its_card_in_the_queue_column(world):
    found = tracker(world)
    found.create_issue("Build this", "Body.", queue=True)
    assert world.jira.status("PROJ-1") == "Ready for factory"


def test_closing_moves_the_card_to_done_or_rejected(world):
    found = tracker(world)
    done, dropped = world.jira.ticket(), world.jira.ticket()
    assert found.close_issue(done) is True
    assert found.close_issue(done) is False
    assert found.close_issue(dropped, "not planned") is True
    assert (world.jira.status(done), world.jira.status(dropped)) == ("Done", "Rejected")


# ---- Comments -------------------------------------------------------------------


def test_comments_say_which_ones_the_factory_wrote(world):
    found = tracker(world)
    key = world.jira.ticket()
    found.comment(key, "### Factory: a question\n\nWhich one?")
    world.jira.comment(key, "The first one.", "alice")
    found.comment(key, "I agree with Alice.", factory=False)
    comments = found.comments(key)
    assert [c["factory"] for c in comments] == [True, False, False]
    assert [c["author"]["login"] for c in comments] == ["op", "alice", "op"]
    assert comments[0]["body"] == "### Factory: a question\n\nWhich one?"
    assert comments[1]["body"] == "The first one."
    assert len({c["id"] for c in comments}) == 3
    assert [c["factory"] for c in found.issue(key)["comments"]] == [True, False, False]


def test_the_factorys_comment_is_a_formatted_document(world):
    found = tracker(world)
    key = world.jira.ticket()
    found.comment(key, "### Factory: rejected\n\nNot in scope.")
    stored = world.jira.issues[key]["comments"][0]["body"]
    assert [block["type"] for block in stored["content"]] == ["heading", "paragraph"]


def test_comments_are_read_across_pages(world):
    found = tracker(world)
    key = world.jira.ticket()
    world.jira.page_size = 2
    for n in range(5):
        world.jira.comment(key, f"Comment {n}", "alice")
    assert [c["body"] for c in found.comments(key)] == [f"Comment {n}" for n in range(5)]


# ---- People ---------------------------------------------------------------------


def test_the_operator_is_the_account_the_token_belongs_to(world):
    assert tracker(world).operator() == "op"


def test_people_are_shown_by_name(world):
    found = tracker(world)
    assert found.mention("op") == "Olivia Operator"
    assert found.mention("alice") == "Alice Author"
    assert found.mention("alice", "Alice A.") == "Alice A."
    assert found.mention("nobody") == "nobody"


def test_a_maintainer_is_anyone_who_can_be_assigned_the_issue(world):
    found = tracker(world)
    key = world.jira.ticket()
    assert found.can_write("op", key) is True
    assert found.can_write("maint", key) is True
    assert found.can_write("bob", key) is False
    found.can_write("maint", key)
    assert len(world.jira.called("GET", "assignable")) == 2


def test_a_maintainers_list_narrows_who_may_answer(world):
    found = tracker(world, settings='maintainers = ["bob"]')
    key = world.jira.ticket()
    assert found.can_write("bob", key) is True
    assert found.can_write("maint", key) is False
    assert found.can_write("op", key) is True
    assert world.jira.called("GET", "assignable") == []


def test_waiting_for_a_person_needs_no_label(world):
    found = tracker(world)
    key = world.jira.ticket()
    assert found.waiting_on(key, "author") is None
    found.waiting_off(key, None)
    found.dequeue(key)
    assert world.jira.called("POST", "") == []


# ---- Moving the card --------------------------------------------------------------


def test_moving_puts_the_card_in_the_steps_status(world):
    found = tracker(world)
    key = world.jira.ticket()
    found.move(key, "build")
    assert world.jira.status(key) == "Build"
    assert world.jira.moves(key) == [("op", "Ready for factory", "Build")]


def test_an_end_state_has_a_status_too(world):
    found = tracker(world)
    key = world.jira.ticket()
    found.move(key, "rejected")
    assert world.jira.status(key) == "Rejected"


def test_an_unmapped_step_leaves_the_card_where_it_is(world):
    found = tracker(world, columns=JIRA_COLUMNS.replace('review = "Review"\n', ""))
    key = world.jira.ticket(status="Build")
    found.move(key, "review")
    assert world.jira.status(key) == "Build"
    assert world.jira.calls == []


def test_a_card_already_in_place_is_not_moved_again(world):
    found = tracker(world)
    key = world.jira.ticket()
    found.move(key, "build")
    found.move(key, "build")
    assert len(world.jira.moves(key)) == 1


def test_a_move_the_workflow_does_not_allow_is_refused_with_both_statuses(world):
    found = tracker(world)
    key = world.jira.ticket()
    world.jira.workflow = {"Ready for factory": ["Intake"]}
    with pytest.raises(Refused) as caught:
        found.move(key, "build")
    assert "`Ready for factory`" in str(caught.value)
    assert "`Build`" in str(caught.value)
    assert world.jira.status(key) == "Ready for factory"


# ---- Credentials and failures -------------------------------------------------------


def test_missing_credentials_are_named_by_their_variable(world, monkeypatch):
    found = tracker(world)
    monkeypatch.delenv("JIRA_API_TOKEN")
    with pytest.raises(Refused, match="JIRA_API_TOKEN"):
        found.operator()


def test_refused_credentials_are_reported_without_the_token(world, monkeypatch):
    found = tracker(world)
    monkeypatch.setenv("JIRA_API_TOKEN", "a-wrong-token")
    with pytest.raises(Refused) as caught:
        found.operator()
    assert "JIRA_EMAIL" in str(caught.value)
    assert "a-wrong-token" not in str(caught.value)


def test_a_site_that_does_not_answer_is_refused(world):
    found = tracker(world)
    world.jira.stop()
    site = world.jira.url
    world.jira = None
    with pytest.raises(Refused, match=f"could not reach Jira at {site}"):
        found.operator()


def test_an_error_from_jira_is_passed_on_in_its_own_words(world):
    found = tracker(world)
    world.jira.fail = ("/rest/api/3/myself", 500)
    with pytest.raises(Refused, match="stub jira: told to fail"):
        found.operator()


# ---- The pull request's link to the issue ---------------------------------------------


def test_the_pull_request_body_starts_with_refs_and_the_key(world):
    found = tracker(world)
    assert found.pr_link_problem("Refs PROJ-1\n\nText.", "PROJ-1") is None
    assert "must start with `Refs PROJ-1`" in found.pr_link_problem("Refs #1\n\nText.", "PROJ-1")


def test_the_pull_request_title_carries_the_key(world):
    found = tracker(world)
    assert found.pr_title_problem("Add dark mode [PROJ-1]", "PROJ-1") is None
    assert "PROJ-1" in found.pr_title_problem("Add dark mode", "PROJ-1")
    assert "PROJ-1" in found.pr_title_problem("Add dark mode [PROJ-12]", "PROJ-1")


def test_the_branch_name_carries_the_key(world):
    assert tracker(world).branch_tag("PROJ-1") == "PROJ-1-"


def test_the_build_agent_is_told_to_put_the_key_in_the_body_and_the_title(world):
    rule = tracker(world).pr_link_rule()
    assert "`Refs <issue key>`" in rule
    assert "title" in rule


# ---- Install and doctor ---------------------------------------------------------------


def test_installing_with_jira_creates_no_github_labels(world):
    found = tracker(world)
    said = []
    found.install(said.append)
    install.create_labels(config.load(world.repo), said.append)
    assert world.calls("label") == []
    assert world.jira.called("POST", "") == []


def test_doctor_says_which_jira_account_is_signed_in(world):
    found = tracker(world)
    ok, failed = [], []
    found.doctor(ok.append, ok.append, failed.append)
    assert ok == [f"Jira: signed in to {world.jira.url} as Olivia Operator"]
    assert failed == []


def test_doctor_fails_when_jira_refuses_the_credentials(world, monkeypatch):
    found = tracker(world)
    monkeypatch.setenv("JIRA_API_TOKEN", "a-wrong-token")
    ok, failed = [], []
    found.doctor(ok.append, ok.append, failed.append)
    assert ok == []
    assert "JIRA_API_TOKEN" in failed[0]
