"""One queue and one dashboard over several installed factories."""

import json
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import CLI, ROOT, git
from modfactory.cli import main
from modfactory.fleet import Fleet, Project, handler
from modfactory.util import Refused


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
    fleet_dir: Path = tmp_path / "fleet"
    fleet_dir.mkdir()
    monkeypatch.chdir(fleet_dir)
    assert main(["fleet", "init"]) == 0
    assert main(["fleet", "init"]) == 1
    assert "already exists" in capsys.readouterr().err

    plain: Path = tmp_path / "plain"
    plain.mkdir()
    assert main(["fleet", "add", str(plain)]) == 1
    assert "not inside a git repository" in capsys.readouterr().err

    bare: Path = tmp_path / "bare"
    bare.mkdir()
    git("init", "-q", "-b", "main", cwd=bare)
    assert main(["fleet", "add", str(bare)]) == 1
    assert "no .factory/bin/mfactory" in capsys.readouterr().err

    repo: Path = checkout(tmp_path, "alpha")
    assert main(["fleet", "add", str(repo)]) == 0
    assert main(["fleet", "add", str(repo)]) == 1
    assert "already in" in capsys.readouterr().err
    assert main(["fleet", "list"]) == 0
    listed: str = capsys.readouterr().out
    assert "alpha" in listed and str(repo.resolve()) in listed and "ok" in listed

    assert main(["fleet", "remove", "alpha"]) == 0
    assert main(["fleet", "list"]) == 0
    assert "no projects yet" in capsys.readouterr().out


def test_two_checkouts_with_the_same_folder_name_get_distinct_labels(tmp_path):
    left: Path = checkout(tmp_path / "a", "app")
    right: Path = checkout(tmp_path / "b", "app")
    file: Path = tmp_path / "fleet.toml"
    group: Fleet = Fleet(file, [])
    group.add(left)
    group.add(right)
    labels: set[str] = {group.label(project) for project in group.projects}
    assert len(labels) == 2
    assert all(label.startswith("app-") for label in labels)


def test_queue_keeps_the_projects_that_succeed(world, tmp_path, monkeypatch, capsys):
    real_factory(world.repo)
    number: int = world.issue(title="Add a widget")
    failed: Path = checkout(tmp_path, "beta", "#!/bin/sh\necho 'mfactory: queue broke' >&2\nexit 1\n")
    fleet_dir: Path = tmp_path / "fleet"
    fleet_dir.mkdir()
    monkeypatch.chdir(fleet_dir)
    assert main(["fleet", "init"]) == 0
    capsys.readouterr()
    group: Fleet = Fleet.find()
    group.projects.append(Project(failed))
    group.save()
    assert main(["fleet", "add", str(world.repo)]) == 0
    capsys.readouterr()

    code: int = main(["fleet", "queue"])
    captured = capsys.readouterr()
    assert code == 1
    assert f"repo  new  {number}  -  -  Add a widget" in captured.out
    assert "beta" in captured.err and "queue broke" in captured.err
    assert "note:" not in captured.out


def test_a_missing_binary_does_not_hide_the_other_project(tmp_path, monkeypatch, capsys):
    good: Path = checkout(tmp_path, "alpha", "#!/bin/sh\nprintf '%s\\n' 'resume  4  001-one  build  round 1'\nexit 0\n")
    missing: Path = tmp_path / "gone"
    fleet_dir: Path = tmp_path / "fleet"
    fleet_dir.mkdir()
    monkeypatch.chdir(fleet_dir)
    file: Path = fleet_dir / "fleet.toml"
    Fleet.init(fleet_dir)
    group: Fleet = Fleet.load(file)
    group.projects = [Project(missing), Project(good)]
    group.save()
    code: int = main(["fleet", "queue"])
    captured = capsys.readouterr()
    assert code == 1
    assert "alpha  resume  4  001-one  build  round 1" in captured.out
    assert "gone" in captured.err and "missing" in captured.err


def test_factory_fleet_follows_factory_fleet_env(tmp_path, monkeypatch):
    repo: Path = checkout(tmp_path, "alpha")
    file: Path = tmp_path / "elsewhere.toml"
    Fleet(file, []).save()
    group: Fleet = Fleet.load(file)
    group.add(repo)
    monkeypatch.setenv("FACTORY_FLEET", str(file))
    found: Fleet = Fleet.find(tmp_path / "no-such")
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
    file: Path = tmp_path / "fleet.toml"
    group: Fleet = Fleet(file, [Project(one.resolve()), Project(two.resolve())])
    group.save()
    server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", 0), handler(file))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield one, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def fetch(url, host=None):
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode(), response.headers


def test_the_fleet_dashboard_serves_both_projects(served):
    _one, base = served
    page, headers = fetch(base + "/")
    assert page == (ROOT / "core/fleet.html").read_text()
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    body: dict = json.loads(fetch(base + "/projects.json")[0])
    by_name: dict = {item["name"]: item for item in body["projects"]}
    assert by_name["one"]["jobs"][0]["title"] == "Alpha"
    assert by_name["one"]["description"] == "A test project"
    assert by_name["two"]["jobs"][0]["title"] == "Beta"
    assert by_name["two"]["jobs"][0]["files"][0]["path"] == "note.md"
    text, kind = fetch(base + "/projects/one/jobs/001-a/note.md")
    assert text == "STATUS: ok\n"
    assert kind["Content-Type"] == "text/plain; charset=utf-8"
    other, _headers = fetch(base + "/projects/two/jobs/001-b/note.md")
    assert other == "STATUS: ok\n"


def test_the_fleet_dashboard_serves_nothing_outside_a_jobs_folder(served):
    _one, base = served
    for path in ("/projects/one/jobs/../../secret.md", "/projects/one/jobs/%2e%2e/%2e%2e/secret.md",
                 "/projects/one/jobs/001-a/job.json", "/fleet.toml"):
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(base + path)
        assert error.value.code == 404, path


def test_the_fleet_dashboard_answers_only_to_its_own_address(served):
    _one, base = served
    for path in ("/projects.json", "/queue.json"):
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(base + path, host="evil.example")
        assert error.value.code == 403, path


def test_the_fleet_dashboard_serves_the_queue_and_caches_it(tmp_path):
    counts: Path = tmp_path / "counts"
    good_script: str = (
        "#!/bin/sh\n"
        f"printf 'x\\n' >> {shlex.quote(str(counts))}\n"
        "printf '%s\\n' 'new  12  -  -  Add a widget'\n"
        "exit 0\n"
    )
    good: Path = checkout(tmp_path, "alpha", good_script)
    bad: Path = checkout(tmp_path, "beta", "#!/bin/sh\necho 'mfactory: queue broke' >&2\nexit 1\n")
    file: Path = tmp_path / "fleet.toml"
    group: Fleet = Fleet(file, [Project(good), Project(bad)])
    group.save()
    server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", 0), handler(file))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base: str = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        first: dict = json.loads(fetch(base + "/queue.json")[0])
        second: dict = json.loads(fetch(base + "/queue.json")[0])
    finally:
        server.shutdown()
        server.server_close()
    expected: dict[str, str] = {
        "project": "alpha", "action": "new", "issue": "12", "job": "-", "step": "-", "note": "Add a widget",
    }
    assert first["rows"] == [expected]
    assert second["rows"] == [expected]
    assert any("beta" in item and "queue broke" in item for item in first["errors"])
    assert counts.read_text().count("x") == 1


def test_the_fleet_page_never_puts_job_text_into_html_unescaped():
    page: str = (ROOT / "core/fleet.html").read_text()
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert unsafe not in page, unsafe


def test_the_fleet_page_loads_nothing_from_elsewhere():
    page: str = (ROOT / "core/fleet.html").read_text()
    for remote in ("<script src", "<link", "@import", "url(http", 'src="http', "<iframe", "<img"):
        assert remote not in page, remote


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to parse the page's script")
def test_the_fleet_script_parses(tmp_path):
    page: str = (ROOT / "core/fleet.html").read_text()
    script: str = page[page.index("<script>") + len("<script>"):page.rindex("</script>")]
    (tmp_path / "page.js").write_text(script)
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["node", "--check", str(tmp_path / "page.js")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_bad_schema_is_refused(tmp_path):
    file: Path = tmp_path / "fleet.toml"
    file.write_text("schema = 2\n")
    with pytest.raises(Refused, match="schema 2"):
        Fleet.load(file)


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
    fleet_dir: Path = tmp_path / "fleet"
    fleet_dir.mkdir()
    monkeypatch.chdir(fleet_dir)
    file: Path = Fleet.init(fleet_dir)
    group: Fleet = Fleet.load(file)
    group.add(alpha)
    group.add(beta)
    group.concurrency = 1
    group.save()
    assert Fleet.load(file).concurrency == 1

    assert main(["fleet", "queue"]) == 0
    capped: str = capsys.readouterr().out
    assert "alpha  resume  1  001-one  build  round 1" in capped
    assert "alpha  wait  2  002-ask  ask-author  waiting for a maintainer" in capped
    assert "beta  hold  3  003-old  build  concurrency 1 reached" in capped
    assert "beta  hold  4  -  -  fleet concurrency 1 reached" in capped

    assert main(["fleet", "queue", "--concurrency", "2"]) == 0
    opened: str = capsys.readouterr().out
    assert "alpha  resume  1  001-one  build  round 1" in opened
    assert "beta  new  4  -  -  Four" in opened
    assert "fleet concurrency" not in opened


def test_the_default_fleet_cap_comes_from_defaults_toml(tmp_path, monkeypatch, capsys):
    script: str = (
        "#!/bin/sh\nprintf '%s\\n' "
        "'new  1  -  -  One' 'new  2  -  -  Two' 'new  3  -  -  Three' 'new  4  -  -  Four'\n"
    )
    repo: Path = checkout(tmp_path, "alpha", script)
    fleet_dir: Path = tmp_path / "fleet"
    fleet_dir.mkdir()
    monkeypatch.chdir(fleet_dir)
    Fleet.init(fleet_dir)
    group: Fleet = Fleet.find()
    group.add(repo)
    assert group.concurrency is None

    assert main(["fleet", "queue"]) == 0
    capped: str = capsys.readouterr().out
    assert "alpha  new  1  -  -  One" in capped
    assert "alpha  new  3  -  -  Three" in capped
    assert "alpha  hold  4  -  -  fleet concurrency 3 reached" in capped

    group.concurrency = 4
    group.save()
    assert main(["fleet", "queue"]) == 0
    opened: str = capsys.readouterr().out
    assert "alpha  new  4  -  -  Four" in opened
    assert "fleet concurrency" not in opened


def test_a_bad_concurrency_is_refused(tmp_path, monkeypatch, capsys):
    file: Path = tmp_path / "fleet.toml"
    file.write_text("schema = 1\nconcurrency = 0\n")
    with pytest.raises(Refused, match="at least 1"):
        Fleet.load(file)
    file.write_text("schema = 1\nconcurrency = true\n")
    with pytest.raises(Refused, match="at least 1"):
        Fleet.load(file)

    fleet_dir: Path = tmp_path / "fleet"
    fleet_dir.mkdir()
    monkeypatch.chdir(fleet_dir)
    Fleet.init(fleet_dir)
    assert main(["fleet", "queue", "--concurrency", "0"]) == 1
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
    broken_jobs: Path = broken / ".factory" / "jobs"
    broken_jobs.mkdir(parents=True)
    (broken_jobs / "graph.json").write_text("{")
    partial_jobs: Path = partial / ".factory" / "jobs"
    partial_jobs.mkdir(parents=True)
    (partial_jobs / "graph.json").write_text('{"steps": [{"name": "intake"}], "end_states": []}')
    group: Fleet = Fleet(
        tmp_path / "fleet.toml", [Project(good), Project(broken), Project(partial), Project(missing)],
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
    assert "graph" not in found["broken"]
    assert "graph" not in found["partial"]
    assert "graph" not in found["missing"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page's script")
def test_a_queue_row_for_an_existing_job_is_not_a_second_card(tmp_path: Path) -> None:
    page: str = (ROOT / "core/fleet.html").read_text()
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
const jobs = [{ id: "001-a", issue: 1 }, { id: "001-d", issue: 4 }];
const cards = queueCards(rows, jobs).map((row) => row.issue);
if (JSON.stringify(cards) !== JSON.stringify(["9", "10"])) throw new Error(JSON.stringify(cards));
"""
    (tmp_path / "queue.js").write_text(script)
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["node", str(tmp_path / "queue.js")], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_subscribe_writes_the_user_fleet_and_a_local_file_wins(tmp_path, monkeypatch, capsys):
    repo: Path = checkout(tmp_path, "alpha")
    monkeypatch.chdir(repo)
    assert main(["fleet", "subscribe"]) == 0
    from modfactory.fleet import Fleet, user_file
    assert user_file().is_file()
    assert repo.resolve().as_posix() in user_file().read_text()
    monkeypatch.chdir(tmp_path)
    assert main(["fleet", "list"]) == 0
    assert "alpha" in capsys.readouterr().out
    assert main(["fleet", "unsubscribe", str(repo)]) == 0

    local: Path = tmp_path / "group"
    local.mkdir()
    other: Path = checkout(local, "beta")
    Fleet.init(local)
    monkeypatch.chdir(other)
    assert main(["fleet", "subscribe"]) == 0
    assert "beta" in (local / "fleet.toml").read_text()
    assert "beta" not in user_file().read_text()
