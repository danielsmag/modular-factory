"""Jira through the Atlassian CLI: the fallback when the REST credentials are not in the environment."""

import pytest
from conftest import JIRA_COLUMNS
from modfactory import config
from modfactory.service import tracker as trackers
from modfactory.service.tracker import jira_acli
from modfactory.util import ConfigError, Refused

PROJECT = 'project = "PROJ"'
SPEC_APPROVAL = """
[steps.spec-approval]
enabled = true
"""
NOTE = "Factory: the card was moved back"


def tracker(world, **use):
    world.use_acli(**use)
    return trackers.make(config.load(world.repo))


def printed(result):
    return [line for line in result.stdout.splitlines() if not line.startswith("note: ")]


def problems(world, settings):
    world.use_jira(settings=settings)
    with pytest.raises(ConfigError) as caught:
        config.load(world.repo)
    return "\n".join(caught.value.problems)


# ---- Which one is used ------------------------------------------------------------------


def test_rest_is_used_while_its_credentials_are_in_the_environment(world):
    world.use_acli()
    world.monkeypatch.setenv("JIRA_EMAIL", "operator@example.test")
    world.monkeypatch.setenv("JIRA_API_TOKEN", "jira-token-for-tests")
    found = trackers.make(config.load(world.repo))
    assert found.via == "rest"
    assert found.operator() == "op"
    assert world.acli_calls() == []


def test_acli_is_the_fallback_when_the_credentials_are_missing(world):
    found = tracker(world)
    assert found.via == "acli"
    assert found.kind == "jira"


def test_one_missing_credential_is_enough_to_fall_back(world):
    world.use_acli()
    world.monkeypatch.setenv("JIRA_EMAIL", "operator@example.test")
    assert trackers.make(config.load(world.repo)).via == "acli"


def test_without_credentials_or_acli_the_error_names_both_ways_in(world):
    world.use_jira()
    world.monkeypatch.delenv("JIRA_API_TOKEN")
    found = trackers.make(config.load(world.repo))
    assert found.via == "rest"
    with pytest.raises(Refused) as caught:
        found.operator()
    assert "JIRA_API_TOKEN" in str(caught.value)
    assert "acli jira auth login" in str(caught.value)


def test_via_rest_never_falls_back(world):
    world.use_acli(settings=PROJECT + '\nvia = "rest"')
    found = trackers.make(config.load(world.repo))
    assert found.via == "rest"
    with pytest.raises(Refused, match="JIRA_EMAIL"):
        found.operator()


def test_via_acli_is_used_even_with_credentials(world):
    world.use_acli(settings=PROJECT + '\nvia = "acli"')
    world.monkeypatch.setenv("JIRA_EMAIL", "operator@example.test")
    world.monkeypatch.setenv("JIRA_API_TOKEN", "jira-token-for-tests")
    assert trackers.make(config.load(world.repo)).via == "acli"


def test_via_acli_without_acli_installed_says_where_to_get_it(world):
    world.use_jira(settings=PROJECT + '\nvia = "acli"')
    with pytest.raises(Refused, match="`acli` isn't installed"):
        trackers.make(config.load(world.repo)).queue()


def test_via_must_be_auto_rest_or_acli(world):
    assert '[tracker.jira].via must be one of "auto", "rest", "acli"' in problems(world, 'via = "curl"')


def test_the_project_must_be_a_project_key(world):
    assert "[tracker.jira].project must be a Jira project key" in problems(world, 'project = "my project"')


def test_the_jql_is_a_filter_without_an_order(world):
    found = problems(world, 'jql = "assignee = currentUser() ORDER BY created"')
    assert "[tracker.jira].jql must be a filter without ORDER BY" in found


def test_acli_needs_the_project_because_it_cannot_read_the_board(world):
    found = tracker(world, settings="")
    with pytest.raises(Refused, match=r"\[tracker\.jira\]\.project"):
        found.queue()


# ---- The queue and issues ------------------------------------------------------------------


def test_the_queue_is_the_projects_cards_in_the_queue_status_oldest_first(world):
    found = tracker(world)
    first, second = world.jira.ticket("First"), world.jira.ticket("Second")
    world.jira.ticket("Not ready", status="Backlog")
    issues = found.queue()
    assert [issue["number"] for issue in issues] == [first, second]
    assert (issues[0]["title"], issues[0]["status"]) == ("First", "Ready for factory")
    assert issues[0]["url"] == f"{world.jira.url}/browse/{first}"
    [search] = world.acli_calls("jira", "workitem", "search")
    jql = search[search.index("--jql") + 1]
    assert jql == 'project = "PROJ" AND status in ("Ready for factory") ORDER BY created ASC'


def test_the_projects_jql_narrows_the_queue_through_acli(world):
    found = tracker(world, settings=PROJECT + '\njql = "assignee = currentUser()"')
    world.jira.ticket("Someone else's")
    mine = world.jira.ticket("Mine", assignee="op")
    assert [issue["number"] for issue in found.queue()] == [mine]


def test_a_search_acli_refuses_for_its_fields_is_asked_again_with_fewer(world):
    found = tracker(world)
    world.monkeypatch.setenv("ACLI_STUB_STRICT_FIELDS", "1")
    key = world.jira.ticket("Queued")
    assert [issue["number"] for issue in found.queue()] == [key]
    assert len(world.acli_calls("jira", "workitem", "search")) == 2


def test_an_issue_and_its_comments_are_read_with_one_command(world):
    found = tracker(world)
    key = world.jira.ticket("Add dark mode", "Users asked.\n\nMake it the default.")
    world.jira.comment(key, "Please keep the old theme too.", "maint")
    issue = found.issue(key)
    assert (issue["number"], issue["title"], issue["state"]) == (key, "Add dark mode", "OPEN")
    assert issue["body"] == "Users asked.\n\nMake it the default."
    assert issue["author"] == {"login": "alice", "name": "Alice Author"}
    assert [(c["author"]["login"], c["body"], c["factory"]) for c in issue["comments"]] == [
        ("maint", "Please keep the old theme too.", False)]
    assert len(world.acli_calls("jira", "workitem")) == 1


def test_comments_beyond_the_first_page_are_listed(world):
    found = tracker(world)
    key = world.jira.ticket()
    world.jira.page_size = 2
    for n in range(5):
        world.jira.comment(key, f"Comment {n}", "alice")
    assert [c["body"] for c in found.comments(key)] == [f"Comment {n}" for n in range(5)]
    assert len(world.acli_calls("jira", "workitem", "comment", "list")) == 1


def test_a_card_outside_the_queue_status_does_not_pass_the_gate(world):
    found = tracker(world)
    waiting, queued = world.jira.ticket(status="Backlog"), world.jira.ticket()
    assert "is in `Backlog`" in found.queue_gate(found.issue(waiting))
    assert found.queue_gate(found.issue(queued)) is None


# ---- Comments --------------------------------------------------------------------------------


def test_a_factory_comment_is_a_formatted_document_that_ends_with_its_signature(world):
    found = tracker(world)
    key = world.jira.ticket()
    found.comment(key, "### Factory: rejected\n\nNot in scope.")
    stored = world.jira.issues[key]["comments"][0]["body"]
    assert [block["type"] for block in stored["content"]] == ["heading", "paragraph", "paragraph"]
    assert world.jira.comments(key)[0].endswith("— mfactory")


def test_comments_say_which_ones_the_factory_wrote(world):
    found = tracker(world)
    key = world.jira.ticket(assignee="op")
    found.comment(key, "### Factory: a question\n\nWhich one?")
    world.jira.comment(key, "The first one.", "alice")
    found.comment(key, "I agree with Alice.", factory=False)
    comments = found.comments(key)
    assert [c["factory"] for c in comments] == [True, False, False]
    assert comments[0]["body"] == "### Factory: a question\n\nWhich one?"
    assert comments[2]["body"] == "I agree with Alice."


def test_a_comment_posted_over_rest_is_known_through_acli_and_the_reverse(world):
    world.use_jira(settings=PROJECT)
    rest = trackers.make(config.load(world.repo))
    key = world.jira.ticket(assignee="op")
    rest.comment(key, "### Factory: asked over REST")
    through_acli = tracker(world)
    through_acli.comment(key, "### Factory: asked through acli")
    assert [c["factory"] for c in through_acli.comments(key)] == [True, True]
    world.monkeypatch.setenv("JIRA_EMAIL", "operator@example.test")
    world.monkeypatch.setenv("JIRA_API_TOKEN", "jira-token-for-tests")
    assert [c["factory"] for c in rest.comments(key)] == [True, True]
    assert rest.comments(key)[1]["body"] == "### Factory: asked through acli"


def test_over_rest_only_the_operators_signed_comment_is_the_factorys(world):
    world.use_jira()
    rest = trackers.make(config.load(world.repo))
    key = world.jira.ticket()
    world.jira.comment(key, "I can sign too.\n\n— mfactory", "bob")
    world.jira.comment(key, "Typed by hand.", "op")
    assert [c["factory"] for c in rest.comments(key)] == [False, False]
    assert world.jira.called("POST", "comment/list") == []


# ---- People ----------------------------------------------------------------------------------


def test_the_operator_is_found_through_a_card_assigned_to_them(world):
    found = tracker(world)
    world.jira.ticket(assignee="op")
    assert found.operator() == "op"
    assert found.mention("op") == "Olivia Operator"


def test_the_operator_is_found_through_a_card_they_reported(world):
    found = tracker(world)
    world.jira.ticket(reporter="op")
    assert found.operator() == "op"


def test_an_account_with_no_cards_cannot_be_told_and_the_error_says_what_to_do(world):
    found = tracker(world)
    world.jira.ticket()
    with pytest.raises(Refused) as caught:
        found.operator()
    assert "acli cannot say which Jira account" in str(caught.value)
    assert "JIRA_EMAIL" in str(caught.value)


def test_through_acli_a_maintainer_is_the_operator_or_a_listed_account(world):
    found = tracker(world, settings=PROJECT + '\nmaintainers = ["maint"]')
    key = world.jira.ticket(assignee="op")
    assert found.can_write("op", key) is True
    assert found.can_write("maint", key) is True
    assert found.can_write("bob", key) is False


def test_through_acli_nobody_else_is_a_maintainer_without_a_list(world):
    found = tracker(world)
    key = world.jira.ticket(assignee="op")
    assert found.can_write("maint", key) is False


def test_a_name_acli_has_not_shown_stays_an_account_id(world):
    found = tracker(world)
    assert found.mention("alice") == "alice"
    found.issue(world.jira.ticket())
    assert found.mention("alice") == "Alice Author"


# ---- Moving the card and filing issues -----------------------------------------------------------


def test_moving_puts_the_card_in_the_steps_status(world):
    found = tracker(world)
    key = world.jira.ticket()
    found.move(key, "build")
    assert world.jira.status(key) == "Build"
    [call] = world.acli_calls("jira", "workitem", "transition")
    assert call[3:] == ["--key", key, "--status", "Build", "--yes"]


def test_a_card_already_in_place_is_not_transitioned(world):
    found = tracker(world)
    key = world.jira.ticket(status="Build")
    found.move(key, "build")
    assert world.acli_calls("jira", "workitem", "transition") == []


def test_a_move_the_workflow_does_not_allow_is_refused_with_both_statuses(world):
    found = tracker(world)
    key = world.jira.ticket()
    world.jira.workflow = {"Ready for factory": ["Intake"]}
    with pytest.raises(Refused) as caught:
        found.move(key, "build")
    assert "from `Ready for factory` to `Build`" in str(caught.value)
    assert "no transition to status 'Build'" in str(caught.value)


def test_filing_an_issue_for_the_queue_creates_it_and_moves_its_card(world):
    found = tracker(world)
    url = found.create_issue("Build this", "### Why\n\nUsers asked.", queue=True)
    assert url == f"{world.jira.url}/browse/PROJ-1"
    created = world.jira.issues["PROJ-1"]
    assert (created["summary"], created["type"], created["status"]) == ("Build this", "Task", "Ready for factory")
    assert created["description"]["content"][0]["type"] == "heading"


def test_closing_moves_the_card_to_done(world):
    found = tracker(world)
    key = world.jira.ticket()
    assert found.close_issue(key) is True
    assert found.close_issue(key) is False
    assert world.jira.status(key) == "Done"


def test_acli_that_is_not_signed_in_says_how_to_sign_in(world):
    found = tracker(world)
    world.monkeypatch.setenv("ACLI_STUB_LOGGED_OUT", "1")
    with pytest.raises(Refused) as caught:
        found.queue()
    assert "acli jira auth login" in str(caught.value)


# ---- A job from start to a pull request -------------------------------------------------------------


def test_a_job_runs_through_acli_and_its_card_follows_every_step(world):
    world.use_acli(settings=PROJECT + '\nmaintainers = ["maint"]')
    key = world.jira.ticket(assignee="op")
    job = world.create(key)
    world.play(job, "ready-to-merge")
    assert world.job(job)["step"] == "ready-to-merge"
    assert [to for who, _, to in world.jira.moves(key)] == [
        "Intake", "Spec", "Build", "Checks", "Review", "Approve", "Ready to merge"]
    assert world.jira.called("GET", "/rest/agile/") == []
    world.jira.comment(key, "/mfactory feedback Use the blue button.", "maint")
    assert world.next(job)[0] == f"{job} {key}: ready-to-merge FEEDBACK -> build"
    assert "From Mia Maintainer at the ready-to-merge step" in (world.dir(job) / "feedback-1.md").read_text()


def test_the_queue_lists_cards_and_jobs_through_acli(world):
    world.use_acli()
    first, second = world.jira.ticket("First", assignee="op"), world.jira.ticket("Second")
    job = world.create(first)
    assert world.queue() == [("resume", first, job, "intake", "round 0"), ("new", second, "-", "-", "Second")]


def test_the_ask_does_not_offer_moving_the_card(world):
    world.use_acli(extra=SPEC_APPROVAL)
    key = world.jira.ticket(assignee="op")
    job = world.create(key)
    world.play(job, "spec-approval")
    ask = world.jira.comments(key)[-1]
    assert "To reply, comment on this issue:" in ask
    assert "move this card" not in ask


def test_a_moved_card_is_put_back_because_acli_cannot_say_who_moved_it(world):
    world.use_acli(extra=SPEC_APPROVAL, settings=PROJECT + '\nmaintainers = ["maint"]')
    key = world.jira.ticket(assignee="op")
    job = world.create(key)
    world.play(job, "spec-approval")
    world.jira.drag(key, "Build", "maint")
    assert world.next(job)[-1].startswith(f"wait {job} {key}: spec-approval")
    assert world.job(job)["step"] == "spec-approval"
    assert world.jira.status(key) == "Spec approval"
    [note] = [text for text in world.jira.comments(key) if NOTE in text]
    assert "This card was moved to `Build`" in note
    assert "acli cannot tell who moved a card" in note
    assert "`/mfactory approve`" in note


def test_a_card_follows_a_changed_mapping_through_acli(world):
    world.use_acli()
    key = world.jira.ticket(assignee="op")
    job = world.create(key)
    world.play(job, "build")
    world.use_acli(columns=JIRA_COLUMNS.replace('build = "Build"', 'build = "Review"'))
    world.queue()
    assert world.jira.status(key) == "Review"
    assert [text for text in world.jira.comments(key) if NOTE in text] == []


# ---- show and doctor ---------------------------------------------------------------------------------


def test_show_lists_the_mapping_and_says_the_board_was_not_checked(world):
    world.use_acli()
    res = world.cli("tracker", "show")
    lines = printed(res)
    assert res.returncode == 0
    assert lines[0] == f"Jira board 7 at {world.jira.url}"
    assert any(line.split()[:3] == ["build", "agent", "Build"] for line in lines)
    assert lines[-1] == ("not checked against Jira: acli cannot read a board's columns or a workflow's "
                         "transitions. Export JIRA_EMAIL and JIRA_API_TOKEN to check them.")


def test_doctor_says_acli_is_in_use_and_what_it_cannot_do(world):
    found = tracker(world)
    world.jira.ticket(assignee="op")
    ok, warn, fail = [], [], []
    found.doctor(ok.append, warn.append, fail.append)
    assert fail == []
    assert ok == [f"Jira: signed in to {world.jira.url} as Olivia Operator, through acli"]
    assert any("a moved card is not an answer" in line for line in warn)
    assert any("[tracker.jira].maintainers" in line and "ready-to-merge" in line for line in warn)


def test_doctor_fails_when_acli_is_not_signed_in(world):
    found = tracker(world)
    world.monkeypatch.setenv("ACLI_STUB_LOGGED_OUT", "1")
    ok, warn, fail = [], [], []
    found.doctor(ok.append, warn.append, fail.append)
    assert ok == []
    assert "acli jira auth login" in fail[0]


def test_doctor_fails_without_the_project(world):
    found = tracker(world, settings="")
    world.jira.ticket(assignee="op")
    ok, warn, fail = [], [], []
    found.doctor(ok.append, warn.append, fail.append)
    assert any("[tracker.jira].project" in line for line in fail)


# ---- Reading what acli prints ---------------------------------------------------------------------------


def test_output_is_read_whether_acli_prints_a_list_or_an_object_holding_one():
    assert jira_acli.items([{"key": "A-1"}], "issues") == [{"key": "A-1"}]
    assert jira_acli.items({"issues": [{"key": "A-1"}]}, "issues") == [{"key": "A-1"}]
    assert jira_acli.items({"values": [{"key": "A-1"}]}, "issues") == [{"key": "A-1"}]
    assert jira_acli.items(None, "issues") == []


def test_json_is_found_after_a_line_of_other_output():
    assert jira_acli.parse('Fetching work items...\n[{"key": "A-1"}]\n') == [{"key": "A-1"}]
    assert jira_acli.parse('{"key": "A-1"}') == {"key": "A-1"}
    with pytest.raises(Refused, match="did not print JSON"):
        jira_acli.parse("✓ done")
