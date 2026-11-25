"""One queue and one dashboard over several installed factories."""

import json
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import CLI, git
from modfactory.cli import main
from modfactory.util import Refused, dashboard_page
from modfactory.workspace import Project, Workspace
from modfactory.workspace.service import DashboardServer, handler


def factory_script(repo: Path, body: str) -> None:
    binary: Path = repo / ".factory" / "bin" / "factory"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(body)
    binary.chmod(0o755)


def checkout(tmp: Path, name: str, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    repo: Path = tmp / name
    repo.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=repo)
    factory_script(repo, body)
    return repo


def real_factory(repo: Path) -> None:
    factory_script(repo, f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(CLI))} \"$@\"\n")


def test_init_add_list_and_remove(tmp_path, monkeypatch, capsys):
    workspace_dir: Path = tmp_path / "workspace"
    workspace_dir.mkdir()
    monkeypatch.chdir(workspace_dir)
    assert main(["workspace", "init"]) == 0
    assert main(["workspace", "init"]) == 1
    assert "already exists" in capsys.readouterr().err

    plain: Path = tmp_path / "plain"
    plain.mkdir()
    assert main(["workspace", "add", str(plain)]) == 1
    assert "not inside a git repository" in capsys.readouterr().err

    bare: Path = tmp_path / "bare"
    bare.mkdir()
    git("init", "-q", "-b", "main", cwd=bare)
    assert main(["workspace", "add", str(bare)]) == 1
    assert "no .factory/bin/mfactory" in capsys.readouterr().err

    repo: Path = checkout(tmp_path, "alpha")
    assert main(["workspace", "add", str(repo)]) == 0
    assert main(["workspace", "add", str(repo)]) == 1
    assert "already in" in capsys.readouterr().err
    assert main(["workspace", "list"]) == 0
    listed: str = capsys.readouterr().out
    assert "alpha" in listed and str(repo.resolve()) in listed and "ok" in listed

    assert main(["workspace", "remove", "alpha"]) == 0
    assert main(["workspace", "list"]) == 0
    assert "no projects yet" in capsys.readouterr().out


def test_two_checkouts_with_the_same_folder_name_get_distinct_labels(tmp_path):
    left: Path = checkout(tmp_path / "a", "app")
    right: Path = checkout(tmp_path / "b", "app")
    file: Path = tmp_path / "workspace.toml"
    group: Workspace = Workspace(file, [])
    group.add(left)
    group.add(right)
    labels: set[str] = {group.label(project) for project in group.projects}
    assert len(labels) == 2
    assert all(label.startswith("app-") for label in labels)


def test_queue_keeps_the_projects_that_succeed(world, tmp_path, monkeypatch, capsys):
    real_factory(world.repo)
    number: int = world.issue(title="Add a widget")
    failed: Path = checkout(tmp_path, "beta", "#!/bin/sh\necho 'mfactory: queue broke' >&2\nexit 1\n")
    workspace_dir: Path = tmp_path / "workspace"
    workspace_dir.mkdir()
    monkeypatch.chdir(workspace_dir)
    assert main(["workspace", "init"]) == 0
    capsys.readouterr()
    group: Workspace = Workspace.find()
    group.projects.append(Project(failed))
    group.save()
    assert main(["workspace", "add", str(world.repo)]) == 0
    capsys.readouterr()

    code: int = main(["workspace", "queue"])
    captured = capsys.readouterr()
    assert code == 1
    assert f"repo  new  {number}  -  -  Add a widget" in captured.out
    assert "beta" in captured.err and "queue broke" in captured.err
    assert "note:" not in captured.out


def test_a_missing_binary_does_not_hide_the_other_project(tmp_path, monkeypatch, capsys):
    good: Path = checkout(tmp_path, "alpha", "#!/bin/sh\nprintf '%s\\n' 'resume  4  001-one  build  round 1'\nexit 0\n")
    missing: Path = tmp_path / "gone"
    workspace_dir: Path = tmp_path / "workspace"
    workspace_dir.mkdir()
    monkeypatch.chdir(workspace_dir)
    file: Path = workspace_dir / "workspace.toml"
    Workspace.init(workspace_dir)
    group: Workspace = Workspace.load(file)
    group.projects = [Project(missing), Project(good)]
    group.save()
    code: int = main(["workspace", "queue"])
    captured = capsys.readouterr()
    assert code == 1
    assert "alpha  resume  4  001-one  build  round 1" in captured.out
    assert "gone" in captured.err and "missing" in captured.err


def test_factory_workspace_follows_factory_workspace_env(tmp_path, monkeypatch):
    repo: Path = checkout(tmp_path, "alpha")
    file: Path = tmp_path / "elsewhere.toml"
    Workspace(file, []).save()
    group: Workspace = Workspace.load(file)
    group.add(repo)
    monkeypatch.setenv("FACTORY_WORKSPACE", str(file))
    found: Workspace = Workspace.find(tmp_path / "no-such")
    assert found.file.resolve() == file.resolve()
    assert found.projects[0].path == repo.resolve()


def write_jobs(repo: Path, job: dict[str, object], index: bool) -> None:
    folder: Path = repo / ".factory" / "jobs" / job["id"]
    folder.mkdir(parents=True)
    (folder / "job.json").write_text(json.dumps(job))
    (folder / "note.md").write_text("STATUS: ok\n")
    if index:
        payload: dict = {
            "project": {"description": "A test project"},
            "concurrency": 3,
            "jobs": [dict(job, files=[{"path": "note.md", "size": 11, "first_line": "STATUS: ok"}])],
        }
        (folder.parent / "index.json").write_text(json.dumps(payload))


@pytest.fixture
def served(tmp_path):
    one: Path = tmp_path / "one"
    two: Path = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    alpha: dict[str, object] = {
        "id": "001-a", "issue": 1, "title": "Alpha", "step": "build", "issue_url": "https://example.test/1",
    }
    write_jobs(one, alpha, True)
    write_jobs(two, {"id": "001-b", "issue": 2, "title": "Beta", "step": "spec"}, False)
    (one / "secret.md").write_text("secret\n")
    file: Path = tmp_path / "workspace.toml"
    group: Workspace = Workspace(file, [Project(one.resolve()), Project(two.resolve())])
    group.save()
    server: DashboardServer = DashboardServer(("127.0.0.1", 0), handler(file))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield one, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def fetch(url, host=None):
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode(), response.headers


def test_the_dashboard_bind_does_not_reverse_resolve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def refuse(name: str = "") -> str:
        raise AssertionError(f"reverse lookup of {name}")

    monkeypatch.setattr(socket, "getfqdn", refuse)
    server: DashboardServer = DashboardServer(("127.0.0.1", 0), handler(tmp_path / "workspace.toml"))
    try:
        assert server.server_name == "127.0.0.1"
        assert server.server_port != 0
    finally:
        server.server_close()


def test_the_workspace_dashboard_serves_both_projects(served):
    one, base = served
    rows: list[dict[str, object]] = [{"key": "run.concurrency", "value": 3, "source": "default"}]
    (one / ".factory" / "jobs" / "config.json").write_text(json.dumps(rows))
    page, headers = fetch(base + "/")
    assert page == dashboard_page().decode()
    assert 'aria-label="Projects"' in page
    assert 'data-view="pipeline"' in page
    assert 'data-view="overview"' in page
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    body: dict = json.loads(fetch(base + "/projects.json")[0])
    by_name: dict = {item["name"]: item for item in body["projects"]}
    assert by_name["one"]["config"] == rows
    assert by_name["two"]["config"] is None
    assert by_name["one"]["jobs"][0]["title"] == "Alpha"
    assert by_name["one"]["description"] == "A test project"
    assert by_name["one"]["concurrency"] == 3
    assert by_name["two"]["concurrency"] is None
    assert by_name["two"]["jobs"][0]["title"] == "Beta"
    assert by_name["two"]["jobs"][0]["files"][0]["path"] == "note.md"
    text, kind = fetch(base + "/projects/one/jobs/001-a/note.md")
    assert text == "STATUS: ok\n"
    assert kind["Content-Type"] == "text/plain; charset=utf-8"
    other, _headers = fetch(base + "/projects/two/jobs/001-b/note.md")
    assert other == "STATUS: ok\n"


def test_the_workspace_dashboard_serves_nothing_outside_a_jobs_folder(served):
    _one, base = served
    for path in ("/projects/one/jobs/../../secret.md", "/projects/one/jobs/%2e%2e/%2e%2e/secret.md",
                 "/projects/one/jobs/001-a/job.json", "/workspace.toml"):
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(base + path)
        assert error.value.code == 404, path


def test_the_workspace_dashboard_answers_only_to_its_own_address(served):
    _one, base = served
    for path in ("/projects.json", "/queue.json"):
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(base + path, host="evil.example")
        assert error.value.code == 403, path


def test_the_workspace_dashboard_serves_the_queue_and_caches_it(tmp_path):
    counts: Path = tmp_path / "counts"
    good_script: str = (
        "#!/bin/sh\n"
        f"printf 'x\\n' >> {shlex.quote(str(counts))}\n"
        "printf '%s\\n' 'new  12  -  -  Add a widget'\n"
        "exit 0\n"
    )
    good: Path = checkout(tmp_path, "alpha", good_script)
    bad: Path = checkout(tmp_path, "beta", "#!/bin/sh\necho 'mfactory: queue broke' >&2\nexit 1\n")
    file: Path = tmp_path / "workspace.toml"
    group: Workspace = Workspace(file, [Project(good), Project(bad)])
    group.save()
    server: DashboardServer = DashboardServer(("127.0.0.1", 0), handler(file))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base: str = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        first: dict = json.loads(fetch(base + "/queue.json")[0])
        second: dict = json.loads(fetch(base + "/queue.json")[0])
    finally:
        server.shutdown()
        server.server_close()
    expected: dict[str, object] = {
        "project": "alpha", "action": "new", "issue": "12", "job": "-", "step": "-", "note": "Add a widget",
        "title": None, "issue_url": None, "created_at": None, "updated_at": None, "author": None, "labels": [],
    }
    assert first["rows"] == [expected]
    assert second["rows"] == [expected]
    assert any("beta" in item and "queue broke" in item for item in first["errors"])
    assert counts.read_text().count("x") == 1


QUEUE_KEYS: set[str] = {
    "project", "action", "issue", "job", "step", "note",
    "title", "issue_url", "created_at", "updated_at", "author", "labels",
}


def serve_file(file: Path) -> tuple[DashboardServer, str]:
    server: DashboardServer = DashboardServer(("127.0.0.1", 0), handler(file))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def test_queue_json_reaches_the_page_and_an_older_core_still_reads(tmp_path, monkeypatch, capsys):
    rows: list[dict[str, object]] = [
        {"action": "new", "issue": 5, "job": "-", "step": "-", "note": "Leak ghp_abcdefgh12345678",
         "title": "Leak ghp_abcdefgh12345678", "issue_url": "https://github.test/acme/app/issues/5",
         "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
         "author": "alice", "labels": ["factory", "ui"]},
        {"action": "hold", "issue": 6, "job": "-", "step": "-", "note": "concurrency 3 reached",
         "title": "Plain", "issue_url": "http://github.test/acme/app/issues/6",
         "created_at": None, "updated_at": None, "author": None, "labels": []},
        {"action": "new", "issue": 7, "job": "-", "step": "-", "note": "Bad link",
         "title": "Bad link", "issue_url": "javascript:alert(1)",
         "created_at": None, "updated_at": None, "author": "bob", "labels": []},
    ]
    alpha: Path = checkout(tmp_path, "alpha", "#!/bin/sh\ncat <<'EOF'\n" + json.dumps(rows) + "\nEOF\n")
    old_script: str = (
        "#!/bin/sh\n"
        "if [ \"$2\" = \"--json\" ]; then\n"
        "  echo 'usage: mfactory [-h]' >&2\n"
        "  echo 'mfactory: error: unrecognized arguments: --json' >&2\n"
        "  exit 2\n"
        "fi\n"
        "printf '%s\\n' 'new  8  -  -  Old core row'\n"
    )
    old: Path = checkout(tmp_path, "old", old_script)
    broken: Path = checkout(tmp_path, "broken", "#!/bin/sh\necho '[not json'\n")
    file: Path = tmp_path / "workspace.toml"
    Workspace(file, [Project(alpha), Project(old), Project(broken)], concurrency=9).save()
    server, base = serve_file(file)
    try:
        body: dict = json.loads(fetch(base + "/queue.json")[0])
    finally:
        server.shutdown()
        server.server_close()
    assert all(set(row) == QUEUE_KEYS for row in body["rows"])
    by_issue: dict[str, dict] = {row["issue"]: row for row in body["rows"]}
    assert set(by_issue) == {"5", "6", "7", "8"}
    assert by_issue["5"]["title"] == "Leak [redacted]"
    assert by_issue["5"]["issue_url"] == "https://github.test/acme/app/issues/5"
    assert by_issue["5"]["labels"] == ["factory", "ui"]
    assert by_issue["5"]["author"] == "alice"
    assert by_issue["5"]["created_at"] == "2026-01-01T00:00:00Z"
    assert by_issue["6"]["issue_url"] is None
    assert by_issue["7"]["issue_url"] is None
    assert by_issue["8"] == {
        "project": "old", "action": "new", "issue": "8", "job": "-", "step": "-", "note": "Old core row",
        "title": None, "issue_url": None, "created_at": None, "updated_at": None, "author": None, "labels": [],
    }
    assert any(error.startswith("broken: ") and "malformed JSON" in error for error in body["errors"])

    monkeypatch.setenv("FACTORY_WORKSPACE", str(file))
    assert main(["workspace", "queue"]) == 1
    printed: str = capsys.readouterr().out
    assert "alpha  new  5  -  -  Leak ghp_abcdefgh12345678\n" in printed
    assert "alpha  hold  6  -  -  concurrency 3 reached\n" in printed
    assert "old  new  8  -  -  Old core row\n" in printed


def test_the_issue_endpoint_reads_one_issue_and_the_queue_never_does(world, tmp_path):
    real_factory(world.repo)
    number: int = world.issue(title="Leak ghp_abcdefgh12345678", body="line one\ntoken ghp_abcdefgh12345678")
    world.comment(number, "first sk-abcdefgh12345678", "bob")
    world.comment(number, "second", "carol")
    file: Path = tmp_path / "workspace.toml"
    Workspace(file, [Project(world.repo)]).save()
    server, base = serve_file(file)
    try:
        queue: dict = json.loads(fetch(base + "/queue.json")[0])
        assert queue["rows"][0]["title"] == "Leak [redacted]"
        assert world.calls("issue list") and not world.calls("issue view")

        issue: dict = json.loads(fetch(base + f"/projects/repo/issues/{number}")[0])
        assert set(issue) == {
            "number", "title", "url", "state", "author", "labels", "created_at", "updated_at",
            "body", "comment_count", "comments",
        }
        assert issue["number"] == number and issue["comment_count"] == 2
        assert issue["title"] == "Leak [redacted]"
        assert issue["body"] == "line one\ntoken [redacted]"
        assert issue["url"] == f"https://github.test/acme/app/issues/{number}"
        assert issue["labels"] == ["factory"] and issue["author"] == "alice" and issue["state"] == "OPEN"
        assert issue["comments"][0] == {"author": "bob", "created_at": "2026-01-01T00:00:00Z",
                                        "body": "first [redacted]"}
        assert len(world.calls("issue view")) == 1

        for path in ("/projects/repo/issues/abc", "/projects/repo/issues/0", "/projects/repo/issues/12;rm",
                     "/projects/repo/issues/12/x", "/projects/repo/issues/-1", f"/projects/nope/issues/{number}"):
            with pytest.raises(urllib.error.HTTPError) as error:
                fetch(base + path)
            assert error.value.code == 404, path
        assert len(world.calls("issue view")) == 1

        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(base + "/projects/repo/issues/999")
        assert error.value.code == 502
        message: str = json.loads(error.value.read())["error"]
        assert message.startswith("Could not load issue #999") and "gh issue view" not in message

        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(base + f"/projects/repo/issues/{number}", host="evil.example")
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_a_bad_schema_is_refused(tmp_path):
    file: Path = tmp_path / "workspace.toml"
    file.write_text("schema = 2\n")
    with pytest.raises(Refused, match="schema 2"):
        Workspace.load(file)


def test_concurrency_keeps_the_first_actionable_rows(tmp_path, monkeypatch, capsys):
    alpha: Path = checkout(
        tmp_path, "alpha",
        "#!/bin/sh\nprintf '%s\\n' 'resume  1  001-one  build  round 1' "
        "'wait  2  002-ask  ask-author  waiting for a maintainer'\n",
    )
    beta: Path = checkout(
        tmp_path, "beta",
        "#!/bin/sh\nprintf '%s\\n' 'hold  3  003-old  build  concurrency 1 reached' 'new  4  -  -  Four'\n",
    )
    workspace_dir: Path = tmp_path / "workspace"
    workspace_dir.mkdir()
    monkeypatch.chdir(workspace_dir)
    file: Path = Workspace.init(workspace_dir)
    group: Workspace = Workspace.load(file)
    group.add(alpha)
    group.add(beta)
    group.concurrency = 1
    group.save()
    assert Workspace.load(file).concurrency == 1

    assert main(["workspace", "queue"]) == 0
    capped: str = capsys.readouterr().out
    assert "alpha  resume  1  001-one  build  round 1" in capped
    assert "alpha  wait  2  002-ask  ask-author  waiting for a maintainer" in capped
    assert "beta  hold  3  003-old  build  concurrency 1 reached" in capped
    assert "beta  hold  4  -  -  workspace concurrency 1 reached" in capped

    assert main(["workspace", "queue", "--concurrency", "2"]) == 0
    opened: str = capsys.readouterr().out
    assert "alpha  resume  1  001-one  build  round 1" in opened
    assert "beta  new  4  -  -  Four" in opened
    assert "workspace concurrency" not in opened


def test_the_default_workspace_cap_comes_from_defaults_toml(tmp_path, monkeypatch, capsys):
    script: str = (
        "#!/bin/sh\nprintf '%s\\n' "
        "'new  1  -  -  One' 'new  2  -  -  Two' 'new  3  -  -  Three' 'new  4  -  -  Four'\n"
    )
    repo: Path = checkout(tmp_path, "alpha", script)
    workspace_dir: Path = tmp_path / "workspace"
    workspace_dir.mkdir()
    monkeypatch.chdir(workspace_dir)
    Workspace.init(workspace_dir)
    group: Workspace = Workspace.find()
    group.add(repo)
    assert group.concurrency is None

    assert main(["workspace", "queue"]) == 0
    capped: str = capsys.readouterr().out
    assert "alpha  new  1  -  -  One" in capped
    assert "alpha  new  3  -  -  Three" in capped
    assert "alpha  hold  4  -  -  workspace concurrency 3 reached" in capped

    group.concurrency = 4
    group.save()
    assert main(["workspace", "queue"]) == 0
    opened: str = capsys.readouterr().out
    assert "alpha  new  4  -  -  Four" in opened
    assert "workspace concurrency" not in opened


def test_a_bad_concurrency_is_refused(tmp_path, monkeypatch, capsys):
    file: Path = tmp_path / "workspace.toml"
    file.write_text("schema = 1\nconcurrency = 0\n")
    with pytest.raises(Refused, match="at least 1"):
        Workspace.load(file)
    file.write_text("schema = 1\nconcurrency = true\n")
    with pytest.raises(Refused, match="at least 1"):
        Workspace.load(file)

    workspace_dir: Path = tmp_path / "workspace"
    workspace_dir.mkdir()
    monkeypatch.chdir(workspace_dir)
    Workspace.init(workspace_dir)
    assert main(["workspace", "queue", "--concurrency", "0"]) == 1
    assert "at least 1" in capsys.readouterr().err


def test_snapshot_attaches_only_a_real_graph(tmp_path: Path) -> None:
    good: Path = checkout(tmp_path, "good")
    broken: Path = checkout(tmp_path, "broken")
    partial: Path = checkout(tmp_path, "partial")
    missing: Path = checkout(tmp_path, "missing")
    graph: dict[str, object] = {
        "start": "intake",
        "end_states": ["done", "failed", "rejected"],
        "steps": [{"name": "intake", "type": "agent", "edges": []}],
    }
    good_jobs: Path = good / ".factory" / "jobs"
    good_jobs.mkdir(parents=True)
    (good_jobs / "graph.json").write_text(json.dumps(graph))
    (good_jobs / "config.json").write_text(json.dumps([
        {"key": "run.concurrency", "value": 3, "source": "default"},
    ]))
    broken_jobs: Path = broken / ".factory" / "jobs"
    broken_jobs.mkdir(parents=True)
    (broken_jobs / "graph.json").write_text("{")
    (broken_jobs / "config.json").write_text("{")
    partial_jobs: Path = partial / ".factory" / "jobs"
    partial_jobs.mkdir(parents=True)
    (partial_jobs / "graph.json").write_text('{"steps": [{"name": "intake"}], "end_states": []}')
    group: Workspace = Workspace(
        tmp_path / "workspace.toml", [Project(good), Project(broken), Project(partial), Project(missing)],
    )
    body: dict[str, object] = group.snapshot()
    raw: object = body["projects"]
    assert isinstance(raw, list)
    found: dict[str, dict[str, object]] = {}
    for item in raw:
        assert isinstance(item, dict)
        found[str(item["id"])] = item
    good_graph: object = found["good"]["graph"]
    assert isinstance(good_graph, dict)
    assert good_graph["start"] == "intake"
    assert found["good"]["config"] == [{"key": "run.concurrency", "value": 3, "source": "default"}]
    assert found["broken"]["config"] is None
    assert "config.json" in str(found["broken"]["error"])
    assert found["missing"]["config"] is None
    assert "graph" not in found["broken"]
    assert "graph" not in found["partial"]
    assert "graph" not in found["missing"]


def test_snapshot_draws_an_empty_project_from_factory_toml(tmp_path: Path) -> None:
    repo: Path = checkout(tmp_path, "empty")
    (repo / "factory.toml").write_text("schema = 1\n")
    group: Workspace = Workspace(tmp_path / "workspace.toml", [Project(repo)])
    body: dict[str, object] = group.snapshot()
    raw: object = body["projects"]
    assert isinstance(raw, list)
    item: object = raw[0]
    assert isinstance(item, dict)
    assert item["jobs"] == []
    drawn: object = item["graph"]
    assert isinstance(drawn, dict)
    steps: object = drawn["steps"]
    assert isinstance(steps, list)
    names: list[object] = [step.get("name") for step in steps if isinstance(step, dict)]
    assert "intake" in names
    assert drawn["end_states"] == ["done", "failed", "rejected"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page's script")
def test_a_queue_row_for_an_existing_job_is_not_a_second_card(tmp_path: Path) -> None:
    page: str = dashboard_page().decode()
    script: str = page.split("<script>", 1)[1].rsplit("</script>", 1)[0].split("/* boot */", 1)[0]
    script += """
const rows = [
  { action: "new", issue: "9", job: "-", note: "New thing" },
  { action: "hold", issue: "9", job: "-", note: "same issue" },
  { action: "hold", issue: "10", job: "-", note: "held" },
  { action: "new", issue: "1", job: "-", note: "already a job" },
  { action: "resume", issue: "3", job: "001-c", note: "resume" },
  { action: "hold", issue: "4", job: "001-d", note: "capped resume" },
];
const listed = [{ id: "001-a", issue: 1 }, { id: "001-d", issue: 4 }];
const cards = queueCards(rows, listed).map((row) => row.issue);
if (JSON.stringify(cards) !== JSON.stringify(["9", "10"])) throw new Error(JSON.stringify(cards));
"""
    (tmp_path / "queue.js").write_text(script)
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["node", str(tmp_path / "queue.js")], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page's script")
def test_job_files_are_sorted_in_the_order_the_job_wrote_them(tmp_path: Path) -> None:
    page: str = dashboard_page().decode()
    script: str = page.split("<script>", 1)[1].rsplit("</script>", 1)[0].split("/* boot */", 1)[0]
    script += """
const want = [
  "issue.md", "intake-1.md", "intake-2.md", "intake.md", "spec.md", "build.md", "progress.md",
  "decision.md", "verify.md", "verify-checks.md", "verify-checks.log", "rejection.md",
  "feedback-1.md", "feedback-2.md",
  "round-1/checks-1.md", "round-1/checks-1.log", "round-1/checks.md", "round-1/checks.log",
  "round-1/review-code.md", "round-1/review-security.md",
  "round-1/debug-2.md", "round-1/debug-10.md", "round-1/merge-checks.md", "round-1/merge-checks.log",
  "round-2/checks.md", "notes.md",
];
const shuffled = want.slice().reverse().map((path) => ({ path }));
const got = sortedFiles(shuffled).map((file) => file.path);
if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(JSON.stringify(got));
const labels = {
  "intake-1.md": "intake (try 1)", "feedback-1.md": "feedback-1.md", "intake.md": "intake.md",
  "round-2/checks-1.md": "R2 checks (try 1)", "round-2/checks-1.log": "R2 checks.log (try 1)",
};
for (const [path, label] of Object.entries(labels)) {
  if (fileLabel(path) !== label) throw new Error(path + " -> " + fileLabel(path));
}
"""
    (tmp_path / "order.js").write_text(script)
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["node", str(tmp_path / "order.js")], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page's script")
def test_queue_cards_carry_issue_metadata_and_an_issue_drawer_is_not_a_job_drawer(tmp_path: Path) -> None:
    page: str = dashboard_page().decode()
    script: str = page.split("<script>", 1)[1].rsplit("</script>", 1)[0].split("/* boot */", 1)[0]
    script += """
globalThis.location = { hash: "#/board", search: "" };
const row = {
  project: "alpha", action: "new", issue: "9", job: "-", step: "-", note: "Widget", title: "Widget",
  issue_url: "https://github.test/acme/app/issues/9", created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z", author: "carol", labels: ["needs-design", "factory"],
};
const passed = queueCards([row], []);
if (JSON.stringify(passed) !== JSON.stringify([row])) throw new Error("metadata changed: " + JSON.stringify(passed));
state.query = "carol";
if (!queueMatches(row)) throw new Error("author did not match");
state.query = "needs-design";
if (!queueMatches(row)) throw new Error("label did not match");
state.query = "nobody";
if (queueMatches(row)) throw new Error("matched nothing");
state.query = "";
state.workspace = true;
state.projects = [{ id: "alpha", name: "alpha", jobs: [{ id: "001-a", issue: 1 }] }];
state.drawer = issueDrawerState(row);
if (sameDrawer({ id: "001-a", project: "alpha" })) throw new Error("issue drawer matched a job");
if (openJob() !== null) throw new Error("openJob found a job for an issue drawer");
if (!sameIssue(row)) throw new Error("issue drawer lost its row");
"""
    (tmp_path / "issue.js").write_text(script)
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["node", str(tmp_path / "issue.js")], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_dashboard_inside_a_listed_repo_opens_that_project(world, monkeypatch):
    real_factory(world.repo)
    monkeypatch.chdir(world.repo)
    assert main(["workspace", "add"]) == 0
    seen: dict[str, object] = {}

    def serve(
        file: Path, port: int = 4747, open_browser: bool = True,
        say: object = print, project: str | None = None,
    ) -> None:
        seen["file"] = file
        seen["project"] = project
        seen["open"] = open_browser

    def serve_one(repo_arg: Path, port: int = 4747, open_browser: bool = True, say: object = print) -> None:
        seen["one"] = repo_arg

    monkeypatch.setattr("modfactory.cli.workspace_http.serve", serve)
    monkeypatch.setattr("modfactory.cli.workspace_http.serve_one", serve_one)
    assert main(["dashboard", "--no-open"]) == 0
    assert seen["project"] == "repo"
    assert seen["open"] is False
    assert seen["file"] is not None
    assert "one" not in seen


def test_dashboard_in_a_plain_git_checkout_opens_the_workspace(world, monkeypatch, tmp_path):
    (world.repo / "factory.toml").unlink()
    monkeypatch.chdir(world.repo)
    file: Path = tmp_path / "mfactory" / "workspace.toml"
    file.parent.mkdir(parents=True)
    group: Workspace = Workspace(file, [])
    group.save()
    seen: dict[str, object] = {}

    def serve(
        workspace_file: Path, port: int = 4747, open_browser: bool = True,
        say: object = print, project: str | None = None,
    ) -> None:
        seen["file"] = workspace_file
        seen["project"] = project

    def serve_one(repo_arg: Path, port: int = 4747, open_browser: bool = True, say: object = print) -> None:
        seen["one"] = repo_arg

    monkeypatch.setattr("modfactory.cli.workspace_http.serve", serve)
    monkeypatch.setattr("modfactory.cli.workspace_http.serve_one", serve_one)
    assert main(["dashboard", "--no-open"]) == 0
    assert seen["file"] == file
    assert seen["project"] is None
    assert "one" not in seen


def test_dashboard_inside_an_unlisted_repo_stays_on_that_repo(world, monkeypatch):
    monkeypatch.chdir(world.repo)
    seen: dict[str, object] = {}

    def serve(
        file: Path, port: int = 4747, open_browser: bool = True,
        say: object = print, project: str | None = None,
    ) -> None:
        seen["workspace"] = True

    def serve_one(repo_arg: Path, port: int = 4747, open_browser: bool = True, say: object = print) -> None:
        seen["repo"] = repo_arg
        seen["open"] = open_browser

    monkeypatch.setattr("modfactory.cli.workspace_http.serve", serve)
    monkeypatch.setattr("modfactory.cli.workspace_http.serve_one", serve_one)
    assert main(["dashboard", "--no-open"]) == 0
    assert seen["repo"] == world.repo.resolve()
    assert seen["open"] is False
    assert "workspace" not in seen


def test_workspace_alone_lists_the_projects(tmp_path, monkeypatch, capsys):
    repo: Path = checkout(tmp_path, "alpha")
    monkeypatch.chdir(repo)
    assert main(["workspace", "add"]) == 0
    capsys.readouterr()
    assert main(["workspace"]) == 0
    assert f"alpha  {repo.resolve()}  ok" in capsys.readouterr().out


def test_add_writes_the_user_workspace_and_a_local_file_wins(tmp_path, monkeypatch, capsys):
    repo: Path = checkout(tmp_path, "alpha")
    monkeypatch.chdir(repo)
    assert main(["workspace", "add"]) == 0
    from modfactory.workspace import Workspace, user_file
    assert user_file().is_file()
    assert repo.resolve().as_posix() in user_file().read_text()
    monkeypatch.chdir(tmp_path)
    assert main(["workspace", "list"]) == 0
    assert "alpha" in capsys.readouterr().out
    assert main(["workspace", "remove", str(repo)]) == 0

    local: Path = tmp_path / "group"
    local.mkdir()
    other: Path = checkout(local, "beta")
    Workspace.init(local)
    monkeypatch.chdir(other)
    assert main(["workspace", "add"]) == 0
    assert "beta" in (local / "workspace.toml").read_text()
    assert "beta" not in user_file().read_text()


def test_the_user_fleet_file_moves_to_workspace_toml(tmp_path, monkeypatch, capsys):
    from modfactory.workspace import user_file
    repo: Path = checkout(tmp_path, "alpha")
    legacy: Path = user_file().with_name("fleet.toml")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(f'schema = 1\n\n[[projects]]\npath = "{repo.resolve()}"\n')
    monkeypatch.chdir(tmp_path)
    assert main(["workspace", "list"]) == 0
    captured = capsys.readouterr()
    assert "moved fleet.toml" in captured.err
    assert "alpha" in captured.out
    assert user_file().is_file() and not legacy.exists()


def test_a_project_binary_hands_dashboard_commands_to_the_global_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("modfactory.cli.has_dashboard_page", lambda: False)
    monkeypatch.setattr("modfactory.cli.global_binary", lambda: None)
    for argv in (["dashboard"], ["start", "dashboard"], ["stop"]):
        assert main(argv) == 1
        assert "install.sh --global" in capsys.readouterr().err

    binary: Path = tmp_path / "home" / "core" / "bin" / "mfactory"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    calls: list[list[str]] = []

    def execv(path: str, args: list[str]) -> None:
        calls.append(args)
        raise SystemExit(0)

    monkeypatch.setattr("modfactory.cli.global_binary", lambda: binary)
    monkeypatch.setattr("modfactory.cli.os.execv", execv)
    with pytest.raises(SystemExit):
        main(["start", "dashboard", "--no-open"])
    assert calls == [[sys.executable, str(binary), "start", "dashboard", "--no-open"]]


def test_other_commands_do_not_hand_off(monkeypatch, capsys):
    monkeypatch.setattr("modfactory.cli.has_dashboard_page", lambda: False)
    monkeypatch.setattr("modfactory.cli.global_binary", lambda: None)
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip()


def write_index(repo: Path, index: dict[str, object]) -> None:
    jobs_dir: Path = repo / ".factory" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "index.json").write_text(json.dumps(index))


def test_old_and_newer_job_files_get_a_notice_instead_of_breaking_the_page(tmp_path):
    from modfactory.store import SCHEMA
    from modfactory.workspace.jobs import build_snapshot
    old: Path = checkout(tmp_path, "old")
    write_index(old, {"jobs": [{"id": "001-a", "step": "spec"}]})
    newer: Path = checkout(tmp_path, "newer")
    write_index(newer, {"schema": SCHEMA + 1, "jobs": [{"id": "001-b"}]})
    current: Path = checkout(tmp_path, "current")
    write_index(current, {"schema": SCHEMA, "jobs": [{"id": "001-c"}]})
    empty: Path = checkout(tmp_path, "empty")
    group: Workspace = Workspace(tmp_path / "workspace.toml", [Project(old), Project(newer), Project(current),
                                                               Project(empty)])
    items: dict[str, dict[str, object]] = {item["id"]: item for item in group.snapshot()["projects"]}
    assert "older factory" in str(items["old"]["notice"])
    assert [job["id"] for job in items["old"]["jobs"]] == ["001-a"]
    assert "newer factory" in str(items["newer"]["notice"])
    assert items["newer"]["jobs"] == []
    assert items["current"]["notice"] is None
    assert items["empty"]["notice"] is None
    assert build_snapshot([], lambda project: "") == {"projects": []}


def test_normalize_job_fills_what_an_old_factory_did_not_write():
    from modfactory.workspace.jobs import normalize_job
    assert normalize_job({"title": "no id"}) is None
    assert normalize_job("not a job") is None
    job: dict[str, object] | None = normalize_job({"id": "004-x", "history": [1, {"step": "spec"}], "round": True})
    assert job is not None
    assert job["title"] == "004-x"
    assert job["step"] == ""
    assert job["issue"] is None
    assert job["round"] == 0
    assert job["history"] == [{"step": "spec"}]
    assert job["files"] == []


def test_saved_jobs_and_the_index_carry_the_schema(world):
    from modfactory.store import SCHEMA
    factory = world.factory()
    job: dict[str, object] = {"id": "001-x", "issue": 1, "step": "spec", "history": []}
    factory.store.path("001-x").mkdir(parents=True)
    factory.store.save(job)
    assert json.loads((factory.store.path("001-x") / "job.json").read_text())["schema"] == SCHEMA
    assert json.loads((factory.store.dir / "index.json").read_text())["schema"] == SCHEMA
