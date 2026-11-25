"""What the dashboard is told about a project whose tracker is Jira."""

import json
from http import HTTPStatus

from modfactory.workspace import jobs as workspace_jobs
from modfactory.workspace import service as workspace_service
from modfactory.workspace.project import Project


def index(world):
    return json.loads((world.repo / ".factory" / "jobs" / "index.json").read_text())


def test_the_drawer_shows_a_jira_issue_with_names_for_people(world):
    world.use_jira()
    key = world.jira.ticket("Add dark mode", "Users asked for it.")
    world.jira.comment(key, "Please keep the old theme too.", "maint")
    status, payload = workspace_service.issue_response(Project(world.repo), key.lower())
    assert status == HTTPStatus.OK
    assert payload["number"] == key
    assert payload["title"] == "Add dark mode"
    assert payload["body"] == "Users asked for it."
    assert payload["author"] == "Alice Author"
    assert payload["comments"] == [{"author": "Mia Maintainer", "created_at": "2026-01-03T00:00:00.000+0000",
                                    "body": "Please keep the old theme too."}]


def test_an_issue_jira_cannot_show_is_an_error_that_names_jira(world):
    world.use_jira()
    status, payload = workspace_service.issue_response(Project(world.repo), "PROJ-99")
    assert status == HTTPStatus.BAD_GATEWAY
    assert payload["error"].startswith("Could not load issue PROJ-99 from Jira: ")


def test_an_issue_github_cannot_show_still_names_github(world):
    status, payload = workspace_service.issue_response(Project(world.repo), "99")
    assert status == HTTPStatus.BAD_GATEWAY
    assert payload["error"].startswith("Could not load issue #99 from GitHub: ")


def test_a_github_drawer_keeps_logins_for_people(world):
    number = world.issue(author="alice")
    _, payload = workspace_service.issue_response(Project(world.repo), str(number))
    assert (payload["number"], payload["author"]) == (number, "alice")


def test_the_index_says_how_an_issue_joins_a_jira_queue(world):
    world.use_jira()
    world.create(world.jira.ticket())
    assert index(world)["tracker"] == {"kind": "jira", "label": "Jira",
                                       "enqueue": "Move a card to `Ready for factory` on board 7"}


def test_the_index_says_how_an_issue_joins_a_github_queue(world):
    world.start(until="spec")
    assert index(world)["tracker"] == {"kind": "github", "label": "GitHub", "enqueue": "Label an issue `factory`"}


def test_the_workspace_passes_the_tracker_to_the_page(world):
    world.use_jira()
    world.create(world.jira.ticket())
    [project] = workspace_jobs.build_snapshot([Project(world.repo)], lambda p: "app")["projects"]
    assert project["tracker"]["kind"] == "jira"
    assert project["jobs"][0]["issue"] == "PROJ-1"


def test_queue_json_names_the_author_and_the_key(world):
    world.use_jira()
    key = world.jira.ticket("Add dark mode")
    out = world.cli("queue", "--json").stdout
    [row] = json.loads(out[out.index("["):])
    assert (row["issue"], row["author"], row["title"]) == (key, "Alice Author", "Add dark mode")
    assert row["issue_url"] == f"{world.jira.url}/browse/{key}"
