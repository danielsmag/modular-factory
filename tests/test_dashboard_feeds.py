"""The dashboard server's routes, and the Jira boards and GitHub issues it reads for the Board."""

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime

import pytest
from conftest import ROOT
from modfactory.service.gh import GitHub
from modfactory.service.tracker import jira as jira_tracker
from modfactory.service.tracker.jira_api import Client
from modfactory.util import Refused
from modfactory.workspace import feeds
from modfactory.workspace.project import Project
from modfactory.workspace.queue import collect_queue
from modfactory.workspace.service import DashboardServer, Feeds, issue_response, one_handler

TODAY = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


@pytest.fixture
def base(world):
    """The address of a dashboard serving `world.repo` on its own."""
    server = DashboardServer(("127.0.0.1", 0), one_handler(world.repo))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(url, host=None, method="GET"):
    """(status, body, headers) of one request; an HTTP error is an answer too."""
    request = urllib.request.Request(url, method=method, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(request, timeout=10) as res:
            return res.status, res.read().decode(), res.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(), error.headers


# ---- Routes ---------------------------------------------------------------------


def test_unknown_paths_and_methods_are_refused(world, base):
    name = world.repo.name
    for path in ("/nope", f"/projects/{name}/nope", f"/projects/{name}/issues/not%20a%20key", "/projects/x/issues/1"):
        assert get(base + path)[0] == 404, path
    assert get(base + "/projects.json", host="evil.example")[0] == 403
    assert get(base + "/projects.json", method="POST")[0] == 501


def test_the_routes_answer_with_json(world, base):
    job = world.start(until="spec")
    name = world.repo.name
    status, body, headers = get(base + "/queue.json")
    assert status == 200 and headers["Content-Type"] == "application/json; charset=utf-8"
    assert sorted(json.loads(body)) == ["errors", "rows"]
    status, body, _ = get(f"{base}/projects/{name}/issues/1")
    assert status == 200 and json.loads(body)["title"] == "Add a thing"
    status, body, _ = get(f"{base}/projects/{name}/jobs/{job}/activity")
    assert status == 200 and "runs" in json.loads(body)
    assert "no-store" in get(base + "/projects.json")[2]["Cache-Control"]


# ---- Reading Jira boards ----------------------------------------------------------


def client(world):
    return Client(world.jira.url, "JIRA_EMAIL", "JIRA_API_TOKEN")


def two_boards(world):
    """The factory's board 7 (PROJ, kanban) and a scrum board 8 (PLAT)."""
    jira = world.use_both()
    jira.add_board(8, "Platform", "PLAT", kind="scrum", columns=["To Do", "In Progress", "Done"])
    return jira


def keys(view):
    return [card["key"] for column in view["columns"] for card in column["cards"]]


def test_boards_lists_every_board_the_account_sees(world):
    jira = two_boards(world)
    jira.page_size = 1
    assert jira_tracker.boards(client(world)) == [
        {"id": 7, "name": "Factory board", "type": "kanban", "project": "PROJ"},
        {"id": 8, "name": "Platform", "type": "scrum", "project": "PLAT"}]


def test_board_view_puts_cards_in_columns_by_status_id(world):
    jira = two_boards(world)
    queued = jira.ticket("Queued", status="Ready for factory")
    building = jira.ticket("Building", status="Build", assignee="maint", priority="High", kind="Bug", labels=["ui"])
    archived = jira.ticket("Archived", status="Archived", updated=TODAY)  # Jira leaves a card on no column out
    view = jira_tracker.board_view(client(world), 7)
    assert view["board"] == {"id": 7, "name": "Factory board", "type": "kanban"}
    assert [column["name"] for column in view["columns"]][:3] == ["Backlog", "Ready for factory", "Intake"]
    cards = {column["name"]: column["cards"] for column in view["columns"]}
    assert [card["key"] for card in cards["Ready for factory"]] == [queued]
    assert cards["Build"] == [{
        "key": building, "title": "Building", "url": f"{jira.url}/browse/{building}", "status": "Build",
        "type": "Bug", "priority": "High", "assignee": "Mia Maintainer", "labels": ["ui"],
        "updated_at": "2026-01-02T00:00:00.000+0000"}]
    assert archived not in keys(view)
    assert (view["shown"], view["hidden"], view["truncated"]) == (2, 0, False)


def test_board_view_leaves_out_old_done_cards(world):
    jira = two_boards(world)
    old = jira.ticket("Done long ago", status="Done")
    new = jira.ticket("Done today", status="Done", updated=TODAY)
    found = keys(jira_tracker.board_view(client(world), 7))
    assert new in found and old not in found


def test_scrum_board_shows_the_open_sprint(world):
    jira = two_boards(world)
    in_sprint = jira.ticket("In the sprint", status="In Progress", project="PLAT", sprint=True)
    jira.ticket("Not yet planned", status="To Do", project="PLAT")
    jira.ticket("On the other board", status="Build")
    view = jira_tracker.board_view(client(world), 8)
    assert [column["name"] for column in view["columns"]] == ["To Do", "In Progress", "Done"]
    assert keys(view) == [in_sprint]


def test_board_view_caps_cards_and_reports_the_total(world):
    jira = two_boards(world)
    for number in range(3):
        jira.ticket(f"Card {number}", status="Build")
    view = jira_tracker.board_view(client(world), 7, limit=2)
    assert (view["shown"], view["total"], view["truncated"]) == (2, 3, True)


def test_readers_take_their_clients_and_import_no_server():
    for path in ("core/lib/modfactory/workspace/feeds.py", "core/lib/modfactory/service/tracker/jira.py",
                 "core/lib/modfactory/service/gh.py"):
        source = (ROOT / path).read_text()
        assert "http.server" not in source and "BaseHTTPRequestHandler" not in source, path


# ---- Reading GitHub issues --------------------------------------------------------


def test_github_issues_lists_open_issues_newest_first_with_me(world):
    older, newer, closed = world.issue("Older"), world.issue("Newer"), world.issue("Closed")

    def change(state):
        state["issues"][str(newer)]["updatedAt"] = "2026-02-01T00:00:00Z"
        state["issues"][str(newer)]["assignees"] = [{"login": "operator"}]
        state["issues"][str(closed)]["state"] = "CLOSED"
    world.set_gh(change)
    found = feeds.github_issues(GitHub(world.repo), lambda: "operator")
    assert found["me"] == "operator" and found["repo_url"] == "https://github.test/acme/app"
    assert [issue["number"] for issue in found["issues"]] == [newer, older]
    assert found["issues"][0] == {
        "number": newer, "title": "Newer", "url": f"https://github.test/acme/app/issues/{newer}",
        "author": "alice", "assignees": ["operator"], "labels": ["factory"],
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-02-01T00:00:00Z"}
    assert found["truncated"] is False


def test_github_issues_say_when_there_are_more_than_shown(world):
    for title in ("One", "Two", "Three"):
        world.issue(title)
    found = feeds.github_issues(GitHub(world.repo), lambda: "operator", limit=2)
    assert len(found["issues"]) == 2 and found["truncated"] is True


# ---- The cache --------------------------------------------------------------------


def test_feed_cache_reuses_answers_and_fresh_skips_them():
    now = [100.0]
    cache = feeds.FeedCache(ttl=60, error_ttl=15, size=2, clock=lambda: now[0])
    calls = []

    def load():
        calls.append("load")
        return {"n": len(calls)}
    assert cache.get("a", load) == {"n": 1}
    assert cache.get("a", load) == {"n": 1}              # within the TTL
    now[0] += 6
    assert cache.get("a", load, fresh=True) == {"n": 2}  # fresh, and the entry is over 5 s old
    now[0] += 1
    assert cache.get("a", load, fresh=True) == {"n": 2}  # fresh, but the entry is new
    now[0] += 61
    assert cache.get("a", load) == {"n": 3}              # the TTL passed


def test_feed_cache_keeps_a_failure_for_a_while_and_holds_few_entries():
    now = [100.0]
    cache = feeds.FeedCache(ttl=60, error_ttl=15, size=2, clock=lambda: now[0])
    tries = []

    def down():
        tries.append("try")
        raise Refused("Jira is down")
    for _ in range(2):
        with pytest.raises(Refused, match="Jira is down"):
            cache.get("board", down)
    assert len(tries) == 1
    now[0] += 16
    with pytest.raises(Refused):
        cache.get("board", down)
    assert len(tries) == 2
    cache.get("one", lambda: 1)
    cache.get("two", lambda: 2)
    assert cache.get("one", lambda: "again") == 1
    cache.get("three", lambda: 3)                         # the oldest entry, "board", goes first
    assert len(cache) == 2


# ---- The routes -------------------------------------------------------------------


def test_feed_routes_over_http(world, base):
    jira = two_boards(world)
    key = jira.ticket("Rotate ghp_abcdefghijklmnop1234", status="Build")
    number = world.issue("A GitHub issue")
    world.set_gh(lambda state: state["issues"][str(number)].update(url="javascript:alert(1)"))
    name = world.repo.name
    status, body, _ = get(f"{base}/projects/{name}/jira/boards")
    found = json.loads(body)
    assert status == 200 and found["configured"] == 7 and found["site"] == jira.url
    assert [board["id"] for board in found["boards"]] == [7, 8]
    assert found["boards"][0]["url"] == f"{jira.url}/secure/RapidBoard.jspa?rapidView=7"
    status, body, _ = get(f"{base}/projects/{name}/jira/boards/7")
    view = json.loads(body)
    [card] = [card for column in view["columns"] for card in column["cards"] if card["key"] == key]
    assert status == 200 and card["title"] == "Rotate [redacted]"
    assert card["url"] == f"{jira.url}/browse/{key}"     # plain http, but on this machine
    status, body, _ = get(f"{base}/projects/{name}/github/issues")
    [issue] = json.loads(body)["issues"]
    assert status == 200 and issue["number"] == number and issue["url"] is None
    for path in (f"/projects/{name}/jira/boards/abc", "/projects/nope/jira/boards", "/projects/nope/github/issues"):
        assert get(base + path)[0] == 404, path


def test_a_second_request_within_the_ttl_asks_jira_nothing(world, base):
    jira = two_boards(world)
    for _ in range(2):
        assert get(f"{base}/projects/{world.repo.name}/jira/boards/7")[0] == 200
    assert len(jira.called("GET", "/rest/agile/1.0/board/7/issue")) == 1


def test_jira_off_says_how_to_turn_it_on(world, base):
    status, body, _ = get(f"{base}/projects/{world.repo.name}/jira/boards")
    assert status == 200 and "[tracker] kind" in json.loads(body)["off"]


def test_acli_only_says_boards_need_rest(world, base):
    world.use_acli()
    status, body, _ = get(f"{base}/projects/{world.repo.name}/jira/boards")
    found = json.loads(body)
    assert status == 200 and found["boards"] == [] and "JIRA_EMAIL" in found["note"]


def test_missing_jira_credentials_name_the_variables(world, base, monkeypatch):
    two_boards(world)
    monkeypatch.delenv("JIRA_EMAIL")
    for path in ("/jira/boards", "/jira/boards/7"):
        status, body, _ = get(f"{base}/projects/{world.repo.name}{path}")
        note = json.loads(body)["note"]
        assert status == 200 and "JIRA_EMAIL" in note and "JIRA_API_TOKEN" not in note, path
        assert "`mfactory stop`" in note and "acli" not in note, path
    assert get(f"{base}/projects/{world.repo.name}/github/issues")[0] == 200


def test_boards_list_through_rest_whenever_both_variables_are_set(world, base):
    two_boards(world)
    world.use_both(settings='via = "acli"\nproject = "PROJ"')  # acli for jobs; the variables are still set
    status, body, _ = get(f"{base}/projects/{world.repo.name}/jira/boards")
    assert status == 200 and [board["id"] for board in json.loads(body)["boards"]] == [7, 8]


def test_error_text_sent_to_the_page_is_redacted():
    def leak():
        raise Refused("Jira said: token ghp_abcdefghijklmnop1234 was refused")
    status, body, _ = Feeds._answer("Could not load board 7 from Jira", leak)
    assert status == 502
    assert json.loads(body)["error"] == "Could not load board 7 from Jira: Jira said: token [redacted] was refused"


def test_workspace_problem_lines_are_redacted(tmp_path):
    repo = tmp_path / "app"
    binary = repo / ".factory" / "bin" / "mfactory"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho '[]'\necho 'problem: Jira: refused ghp_abcdefghijklmnop1234' >&2\n")
    binary.chmod(0o755)
    _, _, errors = collect_queue([Project(repo)], lambda project: "app", None)
    assert errors == ["app: Jira: refused [redacted]"]


class Recorder:
    """A Jira client that answers from memory and keeps the board query it was sent."""

    site = "https://acme.atlassian.net"

    def __init__(self):
        self.jql = None

    def get(self, path, **params):
        if path.endswith("/configuration"):
            return {"columnConfig": {"columns": [{"name": "To Do", "statuses": [{"id": "1"}]}]}}
        return {"id": 7, "name": "Board", "type": "kanban"}

    def pages_with_total(self, method, path, key, body=None, limit=None, **params):
        self.jql = params["jql"]
        return [], 0


def test_the_kanban_query_is_one_clause():
    found = Recorder()
    jira_tracker.board_view(found, 7)
    assert found.jql == "(statusCategory != Done OR updated >= -14d)"


def test_a_board_the_account_cannot_see_is_a_502(world, base):
    two_boards(world)
    status, body, _ = get(f"{base}/projects/{world.repo.name}/jira/boards/99")
    assert status == 502 and json.loads(body)["error"].startswith("Could not load board 99 from Jira")


def test_each_project_uses_its_own_jira_variables(world, base, monkeypatch):
    jira = world.use_both(settings='email_env = "TEAM_JIRA_EMAIL"\ntoken_env = "TEAM_JIRA_TOKEN"')
    monkeypatch.setenv("TEAM_JIRA_EMAIL", os.environ["JIRA_EMAIL"])
    monkeypatch.setenv("TEAM_JIRA_TOKEN", os.environ["JIRA_API_TOKEN"])
    monkeypatch.delenv("JIRA_EMAIL")
    monkeypatch.delenv("JIRA_API_TOKEN")
    assert get(f"{base}/projects/{world.repo.name}/jira/boards/7")[0] == 200
    assert jira.called("GET", "/rest/agile/1.0/board/7/configuration")


def test_drawer_opens_github_issues_in_a_jira_only_project(world):
    world.use_jira()
    world.github_issues = True
    number = world.issue("A GitHub issue next to a Jira board")
    status, body = issue_response(Project(world.repo), str(number))
    assert status == 200 and body["title"] == "A GitHub issue next to a Jira board"
