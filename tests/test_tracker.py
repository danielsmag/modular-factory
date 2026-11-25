"""The tracker: where jobs come from and where people talk to the factory. GitHub issues by default."""

import json
from http import HTTPStatus

import pytest
from modfactory import config, sync
from modfactory.service import tracker as trackers
from modfactory.util import ConfigError, Refused
from modfactory.workspace import jobs as workspace_jobs
from modfactory.workspace import service as workspace_service
from modfactory.workspace.project import Project


def tracker(world):
    return trackers.make(config.load(world.repo))


def link_with_closes(world):
    """Set `[project].issue_link = "closes"` in the test project's factory.toml."""
    path = world.repo / "factory.toml"
    path.write_text(path.read_text().replace("[project]\n", '[project]\nissue_link = "closes"\n', 1))


# ---- Choosing one ------------------------------------------------------------


def test_the_default_tracker_is_github(world):
    assert tracker(world).kind == "github"
    assert world.factory().tracker.kind == "github"


def test_an_unknown_tracker_kind_is_a_config_error(world):
    world.configure('[tracker]\nkind = "trello"\n')
    with pytest.raises(ConfigError, match=r"\[tracker\]\.kind"):
        config.load(world.repo)


# ---- Keys ---------------------------------------------------------------------


def test_a_github_key_is_the_issue_number(world):
    found = tracker(world)
    assert found.key("#12") == 12
    assert found.key("12") == 12
    assert found.key(12) == 12
    assert found.ref(12) == "#12"


def test_a_github_key_that_is_not_a_number_is_refused(world):
    with pytest.raises(Refused, match="PROJ-1"):
        tracker(world).key("PROJ-1")


def test_a_job_is_found_by_a_text_key_whatever_its_case(world):
    job = world.start(until="spec")
    path = world.dir(job) / "job.json"
    data = json.loads(path.read_text())
    data["issue"] = "PROJ-123"
    path.write_text(json.dumps(data))
    store = world.factory().store
    assert store.find("PROJ-123") == job
    assert store.find("proj-123") == job


def test_the_dashboard_keeps_a_text_issue_key(world):
    job = workspace_jobs.normalize_job({"id": "001-a-thing", "issue": "PROJ-123"})
    assert job is not None
    assert job["issue"] == "PROJ-123"
    numbered = workspace_jobs.normalize_job({"id": "001-a-thing", "issue": 12})
    assert numbered is not None
    assert numbered["issue"] == 12


# ---- Issues and the queue -------------------------------------------------------


def test_the_queue_is_the_open_issues_with_the_queue_label(world):
    labeled = world.issue(title="Queued")
    world.issue(title="Not queued", labels=())
    found = tracker(world)
    assert [issue["number"] for issue in found.queue()] == [labeled]


def test_an_unlabeled_issue_does_not_pass_the_queue_gate(world):
    found = tracker(world)
    queued = found.issue(world.issue())
    plain = found.issue(world.issue(labels=()))
    assert found.queue_gate(queued) is None
    assert "isn't labeled `factory`" in found.queue_gate(plain)


def test_a_filed_issue_joins_the_queue_only_when_asked(world):
    found = tracker(world)
    found.create_issue("In the queue", "Body.", queue=True)
    found.create_issue("Just a report", "Body.")
    assert world.labels(1) == ["factory"]
    assert world.labels(2) == []


# ---- Comments -------------------------------------------------------------------


def test_comments_say_which_ones_the_factory_wrote(world):
    number = world.issue()
    found = tracker(world)
    found.comment(number, "### Factory: a question")
    world.comment(number, "An answer.", "alice")
    comments = found.comments(number)
    assert [c["factory"] for c in comments] == [True, False]
    assert comments[0]["body"].strip() == "### Factory: a question"
    assert comments[1]["body"] == "An answer."
    assert [c["factory"] for c in found.issue(number)["comments"]] == [True, False]


def test_a_comment_can_be_posted_as_the_persons_own_words(world):
    number = world.issue()
    found = tracker(world)
    found.comment(number, "My answer.", factory=False)
    assert [c["factory"] for c in found.comments(number)] == [False]


# ---- Waiting for a person ---------------------------------------------------------


def test_waiting_for_the_author_is_shown_with_the_needs_info_label(world):
    number = world.issue()
    found = tracker(world)
    flag = found.waiting_on(number, "author")
    assert flag == "factory:needs-info"
    assert "factory:needs-info" in world.labels(number)
    found.waiting_off(number, flag)
    assert "factory:needs-info" not in world.labels(number)


def test_waiting_for_anyone_else_is_shown_with_the_needs_human_label(world):
    number = world.issue()
    found = tracker(world)
    assert found.waiting_on(number, "maintainer") == "factory:needs-human"
    assert found.waiting_on(number, ["bob"]) == "factory:needs-human"


def test_leaving_the_queue_removes_the_queue_label(world):
    number = world.issue()
    tracker(world).dequeue(number)
    assert world.labels(number) == []


# ---- The pull request's link to the issue -----------------------------------------


def test_the_pull_request_body_must_start_with_refs(world):
    found = tracker(world)
    assert found.pr_link_problem("Refs #12\n\nText.", 12) is None
    assert "must start with `Refs #12`" in found.pr_link_problem("Text.", 12)
    assert "closing keyword" in found.pr_link_problem("Refs #12\n\nFixes #12", 12)


def test_with_closes_any_pull_request_body_passes(world):
    link_with_closes(world)
    assert tracker(world).pr_link_problem("Anything.", 12) is None


def test_the_build_agent_is_told_how_to_link_the_issue(world):
    assert "`Refs #<issue>`" in tracker(world).pr_link_rule()
    link_with_closes(world)
    assert "`Closes #<issue>`" in tracker(world).pr_link_rule()


# ---- Filing an issue from the command line -------------------------------------------


def test_file_opens_an_issue_in_the_queue(world):
    res = world.cli("file", "Add dark mode", input="Users asked for a dark theme.\n")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "https://github.test/acme/app/issues/1"
    issue = world.gh["issues"]["1"]
    assert issue["title"] == "Add dark mode"
    assert issue["body"] == "Users asked for a dark theme.\n"
    assert world.labels(1) == ["factory"]


def test_file_needs_words_for_the_issue(world):
    res = world.cli("file", "Add dark mode", input="")
    assert res.returncode != 0
    assert "stdin" in res.stderr
    assert world.gh["issues"] == {}


# ---- The dashboard's issue drawer -------------------------------------------------------


def test_the_dashboard_reads_an_issue_through_the_projects_tracker(world):
    number = world.issue(title="Shown in the drawer")
    status, payload = workspace_service.issue_response(Project(world.repo), str(number))
    assert status == HTTPStatus.OK
    assert payload["title"] == "Shown in the drawer"


def test_the_dashboard_has_no_issue_for_a_key_the_tracker_does_not_use(world):
    status, payload = workspace_service.issue_response(Project(world.repo), "PROJ-1")
    assert status == HTTPStatus.NOT_FOUND
    assert "PROJ-1" in payload["error"]
    assert world.calls("issue view") == []


def test_the_dashboard_route_takes_numbers_and_named_keys():
    assert workspace_service.ISSUE_KEY.fullmatch("12")
    assert workspace_service.ISSUE_KEY.fullmatch("PROJ-123")
    assert not workspace_service.ISSUE_KEY.fullmatch("../etc")
    assert not workspace_service.ISSUE_KEY.fullmatch("0")


# ---- The orchestrator's skill -----------------------------------------------------------


def test_the_skill_files_a_new_feature_through_the_factory(world):
    text = sync.skill_text()
    assert ".factory/bin/mfactory file" in text
    assert "gh issue create" not in text
