"""The command line, and installing, checking and upgrading the factory in a project."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import ROOT, git
from modfactory import install
from modfactory.cli.maintain import _changelog_note, status_all, upgrade_everything
from modfactory.util import dashboard_page
from modfactory.workspace.service import DashboardServer, one_handler


@pytest.fixture
def bare(world):
    """The project before the factory is installed: no factory.toml."""
    (world.repo / "factory.toml").unlink()
    git("commit", "-qam", "remove factory.toml", cwd=world.repo)
    git("push", "-q", "origin", "main", cwd=world.repo)
    (world.repo / ".gitignore").write_text("node_modules/\n")
    return world


def installed(world):
    return subprocess.run([str(world.repo / ".factory/bin/mfactory"), "version"], capture_output=True, text=True)


# ---- init ------------------------------------------------------------------


def test_init_installs_everything_a_project_needs(bare):
    res = bare.cli("init", "--tool", "claude", "--tool", "codex")
    assert res.returncode == 0, res.stderr
    repo = bare.repo
    for item in ("bin/mfactory", "bin/factory", "lib/modfactory/cli/__init__.py", "roles/build.md", "skill/SKILL.md",
                 "defaults.toml", "templates/factory.toml", "adapters/claude.toml", "VERSION", "LICENSE",
                 "NOTICE"):
        assert (repo / ".factory" / item).exists(), item
    assert not (repo / ".factory" / "web").exists()
    assert not list((repo / ".factory").rglob("__pycache__"))
    assert installed(bare).stdout.strip() == (ROOT / "core/VERSION").read_text().strip()

    toml = (repo / "factory.toml").read_text()
    assert 'enabled = ["claude", "codex"]' in toml and 'base_branch = "main"' in toml
    assert "pr_title_pattern" in toml
    assert "# [steps.merge]\n# enabled = true" in toml
    assert (repo / ".gitignore").read_text() == (
        "node_modules/\n\n# software factory runtime state\n/.factory/jobs/\n/.worktrees/\n")
    assert (repo / ".claude/agents/mfactory-build.md").exists()
    assert (repo / ".codex/agents/mfactory-build.toml").exists()
    assert not (repo / ".cursor").exists()
    assert bare.gh["labels"] == ["factory", "factory:needs-human", "factory:needs-info"]
    assert "Next:" in res.stdout and "/mfactory setup" in res.stdout


def test_install_leaves_existing_labels_alone(bare):
    bare.set_gh(lambda s: s.update(labels=["factory"]))
    res = bare.cli("install")
    assert res.returncode == 0, res.stderr
    assert "ok    label factory already exists" in res.stdout
    assert "ok    GitHub labels created: factory:needs-info, factory:needs-human" in res.stdout
    assert bare.gh["labels"] == ["factory", "factory:needs-human", "factory:needs-info"]
    creates = [call for call in bare.gh["calls"] if call[:2] == ["label", "create"]]
    assert [call[2] for call in creates] == ["factory:needs-info", "factory:needs-human"]
    assert not any("--force" in call for call in creates)


def test_init_again_keeps_factory_toml_and_changes_nothing_else(bare):
    bare.cli("init")
    (bare.repo / "factory.toml").write_text('schema = 1\n[run]\nconcurrency = 7\n')
    before = (bare.repo / ".gitignore").read_text()
    res = bare.cli("init", "--tool", "cursor")
    assert res.returncode == 0
    assert "factory.toml exists and was left as it is" in res.stdout
    assert (bare.repo / "factory.toml").read_text() == 'schema = 1\n[run]\nconcurrency = 7\n'
    assert (bare.repo / ".gitignore").read_text() == before


def test_personal_install_leaves_the_factory_untracked(bare):
    before = (bare.repo / ".gitignore").read_text()
    res = bare.cli("install", "--tool", "claude", "--personal")
    assert res.returncode == 0, res.stderr
    assert (bare.repo / ".gitignore").read_text() == before
    assert ".gitignore" not in res.stdout
    assert "Leave factory.toml, pipelines/, .factory/ and the generated files untracked." in res.stdout
    exclude = Path(git("rev-parse", "--git-path", "info/exclude", cwd=bare.repo))
    if not exclude.is_absolute():
        exclude = bare.repo / exclude
    text = exclude.read_text()
    block = "\n".join((
        "# software factory, this checkout only",
        "/factory.toml",
        "/.factory/",
        "/pipelines/",
        "/.worktrees/",
        "/.claude/agents/mfactory-*",
        "/.claude/agents/factory-*",
        "/.claude/skills/mfactory/",
        "/.cursor/agents/mfactory-*",
        "/.cursor/agents/factory-*",
        "/.agents/skills/mfactory/",
        "/.codex/agents/mfactory-*",
        "/.codex/agents/factory-*",
        "",
    ))
    assert block in text
    for path in ("factory.toml", ".factory/jobs/x", ".claude/agents/mfactory-build.md"):
        ignored = subprocess.run(["git", "check-ignore", "-v", "--", path], cwd=bare.repo,
                                 capture_output=True, text=True)
        assert ignored.returncode == 0, path
        assert ignored.stdout.split(":", 1)[0].endswith("info/exclude"), ignored.stdout
    status = git("status", "--short", cwd=bare.repo)
    assert "factory.toml" not in status
    assert ".factory" not in status
    assert "mfactory-build" not in status

    again = bare.cli("install")
    assert again.returncode == 0, again.stderr
    assert (bare.repo / ".gitignore").read_text() == before
    assert "ok    .git/info/exclude already ignores the factory (personal install)" in again.stdout

    (bare.repo / "factory.toml").write_text(
        'schema = 1\n[project]\nbase_branch = "main"\n[checks]\ncommands = ["true"]\n')
    checked = doctor(bare, "--skip-checks")
    assert "aren't on origin/main yet" not in checked.stdout
    assert ("ok    factory.toml and .factory/ are ignored in this checkout (.git/info/exclude). "
            "Worktrees built from origin/main do not contain them. "
            "A check that needs those files must use the main checkout.") in checked.stdout


def test_init_lists_the_skills_it_finds(bare):
    skill = bare.repo / ".claude/skills/testing/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: testing\ndescription: How tests are written here.\n---\n")
    out = bare.cli("init").stdout
    assert "testing  (.claude/skills/testing/SKILL.md)  How tests are written here." in out


def test_install_sh_installs_from_a_local_copy(bare):
    res = subprocess.run(["bash", str(ROOT / "install.sh"), "--tool", "cursor,codex", "--source", str(ROOT)],
                         cwd=bare.repo, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert 'enabled = ["cursor", "codex"]' in (bare.repo / "factory.toml").read_text()
    assert installed(bare).returncode == 0


def test_install_sh_refuses_outside_a_repository(tmp_path):
    res = subprocess.run(["bash", str(ROOT / "install.sh"), "--source", str(ROOT)], cwd=tmp_path,
                         capture_output=True, text=True,
                         env=dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path.parent)))
    assert res.returncode == 1 and "inside the git repository" in res.stderr


def test_the_installed_cli_works_from_a_worktree_of_a_job(bare):
    bare.cli("init")
    tree = bare.tmp / "tree"
    git("worktree", "add", "-q", "--detach", str(tree), cwd=bare.repo)
    res = subprocess.run([str(bare.repo / ".factory/bin/mfactory"), "graph"], cwd=tree, capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout.startswith("intake ")


# ---- the command line ------------------------------------------------------


def test_errors_are_one_line_and_exit_1(world):
    res = world.cli("next", "999")
    assert (res.returncode, res.stdout, res.stderr) == (1, "", "mfactory: no job matches '999'. Jobs: none\n")


def test_config_problems_are_listed_with_their_keys(world):
    world.configure("[run]\nconcurrency = 0\n")
    res = world.cli("graph")
    assert res.returncode == 1
    assert res.stderr == ("mfactory: factory.toml has 1 problem(s):\n"
                          "  - [run].concurrency must be a whole number of at least 1 (got 0)\n")


def test_outside_a_repository(tmp_path):
    res = subprocess.run([str(ROOT / "core/bin/mfactory"), "queue"], cwd=tmp_path, capture_output=True, text=True,
                         env=dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path.parent)))
    assert res.returncode == 1 and "is not inside a git repository" in res.stderr


def test_a_job_runs_from_the_command_line(world):
    number = world.issue(title="Add search")
    assert world.cli("queue").stdout.splitlines()[-1] == f"new  {number}  -  -  Add search"
    assert world.cli("create", number).stdout == "created 001-add-search for #1 at intake\n"
    out = world.cli("next", "001").stdout.splitlines()
    assert out[0] == "start-agent mfactory-intake" and out[-1] == "stdin: the agent's whole reply"
    reply = "DECISION: ACCEPT\nRISK: low\nSURFACES: none\nRESCOPED: no\n\n## Scope\nSearch.\n"
    assert world.cli("done", "#1", "--reply", input=reply).stdout == "001-add-search #1: intake ACCEPT -> spec\n"
    assert world.cli("route", "1").stdout.strip() == json.dumps(
        {"reviewers": {"code": "mfactory-reviewer-code-low"}}, indent=1)
    missing = world.cli("summary")
    assert missing.returncode != 0 and "invalid choice" in missing.stderr


def test_answer_takes_its_words_from_arguments_or_standard_input(world):
    job = world.start(until="spec-approval", config_text="[steps.spec-approval]\nenabled = true\n")
    world.factory()                                          # let go of the job's lock
    res = world.cli("answer", job, "changes", input="Split it\nin two.\n")
    assert res.stdout == f"{job} #1: spec-approval CHANGES -> spec\n"
    assert "Split it\nin two." in (world.dir(job) / "feedback-1.md").read_text()


def test_queue_says_when_the_generated_files_are_out_of_date(world):
    world.cli("sync")
    assert "note:" not in world.cli("queue").stdout
    world.configure("[steps.debug]\nenabled = true\n")
    assert world.cli("queue").stdout.startswith("note: 1 generated file(s) are out of date with factory.toml.")
    assert world.cli("sync", "--check").returncode == 1
    assert world.cli("sync").stdout == (
        "create  .claude/agents/mfactory-debug.md\n"
        'note: rule "user:CLAUDE.md" is not installed on this machine\n')
    assert world.cli("sync", "--check").returncode == 0


def test_models_and_graph_describe_the_setup(world):
    world.configure('[tools]\nenabled = ["claude", "codex"]\n[steps.debug]\nenabled = true\n')
    models = world.cli("models").stdout.splitlines()
    assert models[0].split() == ["agent", "claude", "codex"]
    assert "mfactory-debug              opus / high      inherit / high" in models
    graph = world.cli("graph").stdout
    assert "gate             checks  PASS -> review | FAIL -> debug (max 2)" in graph
    assert "spec-approval    human   off, continues as APPROVE -> build" in graph
    assert "merge            merge   off, continues as MERGED -> ready-to-verify" in graph
    assert "ready-to-verify  human   off, continues as VERIFY -> verify" in graph


# ---- doctor ----------------------------------------------------------------


def ready(world):
    """Install, commit and push, as the install guide says."""
    world.cli("init")
    world.configure("")
    git("add", "-A", cwd=world.repo)
    git("commit", "-qm", "factory", cwd=world.repo)
    git("push", "-q", "origin", "main", cwd=world.repo)
    return world.repo / ".factory/bin/mfactory"


def doctor(world, *args):
    return subprocess.run([str(world.repo / ".factory/bin/mfactory"), "doctor", *args], cwd=world.repo,
                          capture_output=True, text=True)


def test_doctor_passes_on_a_ready_project(world):
    ready(world)
    res = doctor(world)
    assert res.returncode == 0, res.stdout
    for line in ("ok    gh is signed in",
                 "ok    factory.toml is valid",
                 'ok    pipeline "default" (pipelines/default.yaml) is valid: 9 steps on, '
                 "off: spec-approval, debug, merge, ready-to-verify, verify",
                 "ok    generated agent and skill files match factory.toml", "ok    origin/main can be fetched",
                 "ok    .factory/jobs/ is ignored by git", "ok    GitHub labels exist",
                 "Everything a run needs is in place."):
        assert line in res.stdout, line
    assert "ok    the checks pass on origin/main" in res.stdout
    if shutil.which("claude"):
        assert "ok    Claude Code: " in res.stdout
    else:
        assert "warn  Claude Code: `claude` isn't on PATH" in res.stdout
    assert "still points at it" not in res.stdout
    assert "nothing asks a person to merge" not in res.stdout


def test_doctor_finds_hand_edits_missing_labels_and_failing_checks(world):
    ready(world)
    (world.repo / ".claude/agents/mfactory-build.md").write_text("mine\n")
    world.set_gh(lambda s: s.update(labels=["factory"]))
    (world.repo / "FAIL").write_text("x\n")
    git("add", "FAIL", cwd=world.repo)
    git("commit", "-qm", "break", cwd=world.repo)
    git("push", "-q", "origin", "main", cwd=world.repo)

    res = doctor(world, "--skip-checks")
    assert res.returncode == 1
    assert ("FAIL  .claude/agents/mfactory-build.md differs from what factory.toml generates "
            "(edited by hand, or not synced)") in res.stdout
    assert "FAIL  GitHub labels missing: factory:needs-info, factory:needs-human." in res.stdout
    assert "2 problem(s) to fix." in res.stdout

    world.cli("sync")
    world.set_gh(lambda s: s.update(labels=["factory", "factory:needs-info", "factory:needs-human"]))
    res = doctor(world)
    assert res.returncode == 1
    assert "FAIL  the checks fail on origin/main (exit 1), so every job would fail:" in res.stdout
    assert "$ test ! -f FAIL" in res.stdout


def test_doctor_reports_config_problems_first(world):
    ready(world)
    world.configure("[steps.gate]\non = { PASS = \"nowhere\" }\n")
    res = doctor(world)
    assert res.returncode == 1
    assert 'FAIL  [steps.gate].on.PASS points at unknown step "nowhere".' in res.stdout
    assert "Fix factory.toml first" in res.stdout


def test_doctor_warns_when_an_edge_skips_a_disabled_merge(bare):
    bare.cli("init")
    (bare.repo / "factory.toml").write_text(
        'schema = 1\n[project]\nbase_branch = "main"\n[checks]\ncommands = ["true"]\n'
        '[steps.approve]\non = { APPROVE = "merge" }\n'
        '[steps.ready-to-merge]\nenabled = false\nskip = "REJECT"\n')
    res = doctor(bare, "--skip-checks")
    assert "warn  [steps.merge] is off, but approve.APPROVE still points at it." in res.stdout
    assert "FAIL  [steps.merge]" not in res.stdout


def test_doctor_warns_when_a_listed_skill_is_not_installed(world, monkeypatch):
    ready(world)
    monkeypatch.setenv("HOME", str(world.tmp / "empty-home"))
    (world.tmp / "empty-home").mkdir()
    world.configure('[skills]\nall = ["user:missing", "superpowers:test-driven-development"]\n')
    synced = world.cli("sync")
    assert synced.returncode == 0, synced.stderr
    assert 'note: skill "user:missing" is not installed on this machine' in synced.stdout
    gap = 'skill "superpowers:test-driven-development" is not installed or enabled for Claude Code'
    assert f"note: {gap}" in synced.stdout
    res = doctor(world, "--skip-checks")
    assert 'warn  skill "user:missing" is not installed on this machine' in res.stdout
    assert 'warn  skill "superpowers:test-driven-development" is not installed or enabled for Claude Code' in res.stdout


def test_doctor_warns_when_a_listed_rule_is_not_installed(world, monkeypatch):
    ready(world)
    monkeypatch.setenv("HOME", str(world.tmp / "empty-home"))
    (world.tmp / "empty-home").mkdir()
    world.configure('[rules]\nall = ["user:CLAUDE.md"]\n')
    synced = world.cli("sync")
    assert synced.returncode == 0, synced.stderr
    assert 'note: rule "user:CLAUDE.md" is not installed on this machine' in synced.stdout
    res = doctor(world, "--skip-checks")
    assert 'warn  rule "user:CLAUDE.md" is not installed on this machine' in res.stdout


def test_doctor_warns_when_the_factory_is_not_on_the_base_branch(bare):
    bare.cli("init")
    (bare.repo / "factory.toml").write_text(
        'schema = 1\n[project]\nbase_branch = "main"\n[checks]\ncommands = ["true"]\n')
    res = doctor(bare)
    assert "warn  factory.toml and .factory/ aren't on origin/main yet." in res.stdout


# ---- dashboard server ------------------------------------------------------


@pytest.fixture
def served(world):
    job = world.start(until="spec", title="<img src=x onerror=alert(1)>")
    server = DashboardServer(("127.0.0.1", 0), one_handler(world.repo))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield job, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def fetch(url, host=None):
    request = urllib.request.Request(url, headers={"Host": host} if host else {})
    with urllib.request.urlopen(request, timeout=10) as res:
        return res.read().decode(), res.headers


def test_the_dashboard_serves_the_page_and_live_data(served, world):
    job, base = served
    page, headers = fetch(base + "/")
    assert page == dashboard_page().decode()
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    body = json.loads(fetch(base + "/projects.json")[0])
    project = body["projects"][0]
    assert project["name"] == world.repo.name
    assert project["jobs"][0]["id"] == job and project["jobs"][0]["title"] == "<img src=x onerror=alert(1)>"
    assert project["graph"]["start"] == "intake"
    assert {"key": "run.concurrency", "value": 3, "source": "default"} in project["config"]
    text, headers = fetch(f"{base}/projects/{world.repo.name}/jobs/{job}/intake.md")
    assert text.startswith("DECISION: ACCEPT") and headers["Content-Type"] == "text/plain; charset=utf-8"


def test_the_dashboard_serves_nothing_outside_the_jobs_folder(served, world):
    job, base = served
    (world.repo / "secret.md").write_text("secret\n")
    name = world.repo.name
    for path in (f"/projects/{name}/jobs/{job}/job.json", f"/projects/{name}/jobs/../../secret.md",
                 f"/projects/{name}/jobs/%2e%2e/%2e%2e/secret.md",
                 f"/projects/{name}/jobs/{job}/../../../secret.md", "/factory.toml"):
        with pytest.raises(urllib.error.HTTPError) as error:
            fetch(base + path)
        assert error.value.code == 404, path


def test_the_dashboard_answers_only_to_its_own_address(served):
    _, base = served
    with pytest.raises(urllib.error.HTTPError) as error:
        fetch(base + "/projects.json", host="evil.example")
    assert error.value.code == 403


def test_start_dashboard_serves_until_stop(world):
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    try:
        started = world.cli("start", "dashboard", "--port", port, "--no-open")
        assert started.returncode == 0, started.stderr
        assert url in started.stdout
        with urllib.request.urlopen(url + "projects.json", timeout=5) as response:
            body = json.loads(response.read().decode())
        assert body["projects"][0]["jobs"] == []
        assert body["projects"][0]["name"] == world.repo.name
        again = world.cli("start", "dashboard", "--no-open")
        assert again.returncode == 1
        assert "already running" in again.stderr
        stopped = world.cli("stop", "dashboard")
        assert stopped.returncode == 0, stopped.stderr
        assert f"stopped {url}" in stopped.stdout
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(url, timeout=2)
        quiet = world.cli("stop")
        assert quiet.returncode == 0
        assert "not running" in quiet.stdout
    finally:
        world.cli("stop")


def test_the_dashboard_page_never_puts_job_text_into_html_unescaped():
    page = dashboard_page().decode()
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert unsafe not in page, unsafe


def test_the_dashboard_page_loads_nothing_from_elsewhere():
    page = dashboard_page().decode()
    for remote in ("<script src", "@import", "url(http", 'src="http', "<iframe", "<img"):
        assert remote not in page, remote
    # The tab icon is inlined as a data: URI; any other <link> would load from elsewhere.
    for link in re.findall(r"<link\b[^>]*>", page):
        assert 'href="data:' in link, link


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to parse the page's script")
def test_the_dashboard_script_parses(tmp_path):
    page = dashboard_page().decode()
    script = page[page.index("<script>") + len("<script>"):page.rindex("</script>")]
    (tmp_path / "page.js").write_text(script)
    result = subprocess.run(["node", "--check", str(tmp_path / "page.js")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_upgrade_updates_the_global_cli_and_every_workspace_project(world):
    installed = world.cli("install", "--tool", "cursor")
    assert installed.returncode == 0, installed.stderr
    assert "ok    added to" in installed.stdout
    other: Path = world.tmp / "other"
    shutil.copytree(world.repo, other)
    added = world.cli("workspace", "add", other)
    assert added.returncode == 0, added.stderr
    for repo in (world.repo, other):
        (repo / ".factory" / "VERSION").write_text("0.0.0\n")
    home: Path = Path(os.environ["FACTORY_HOME"])
    (home / "core" / "bin").mkdir(parents=True)
    (home / "core" / "bin" / "mfactory").write_text("")
    (home / "core" / "VERSION").write_text("0.0.0\n")
    (home / ".global").write_text(f"{home}\n")
    result = world.cli("upgrade", "--source", str(ROOT), cwd=world.tmp)
    assert result.returncode == 0, result.stderr + result.stdout
    expected: str = (ROOT / "core" / "VERSION").read_text().strip()
    assert (home / "core" / "VERSION").read_text().strip() == expected
    assert (world.repo / ".factory" / "VERSION").read_text().strip() == expected
    assert (other / ".factory" / "VERSION").read_text().strip() == expected
    assert "global install 0.0.0 ->" in result.stdout
    assert "3 upgraded, 0 current" in result.stdout
    assert str(world.repo.resolve()) in (home / "workspace.toml").read_text()
    assert str(other.resolve()) in (home / "workspace.toml").read_text()


def test_upgrade_with_nothing_installed_refuses(world):
    result = world.cli("upgrade", "--source", str(ROOT), cwd=world.tmp)
    assert result.returncode == 1
    assert "nothing to upgrade" in result.stderr


def _global_home(version: str = "0.0.0\n") -> Path:
    home: Path = Path(os.environ["FACTORY_HOME"])
    (home / "core" / "bin").mkdir(parents=True, exist_ok=True)
    (home / "core" / "bin" / "mfactory").write_text("")
    (home / "core" / "VERSION").write_text(version)
    (home / ".global").write_text(f"{home}\n")
    return home


def test_upgrade_downloads_the_release_once(world, monkeypatch):
    installed = world.cli("install", "--tool", "cursor")
    assert installed.returncode == 0, installed.stderr
    repos: list[Path] = [world.repo]
    for name in ("other", "third"):
        copy: Path = world.tmp / name
        shutil.copytree(world.repo, copy)
        repos.append(copy)
    for repo in repos[1:]:
        added = world.cli("workspace", "add", repo)
        assert added.returncode == 0, added.stderr
    for repo in repos:
        (repo / ".factory" / "VERSION").write_text("0.0.0\n")
    _global_home()
    calls: list[str] = []

    def fake_fetch(ref: str, into: str) -> Path:
        calls.append(ref)
        return ROOT

    monkeypatch.chdir(world.tmp)
    monkeypatch.setattr("modfactory.cli.maintain._installed_checkout", lambda: None)
    monkeypatch.setattr("modfactory.cli.maintain.fetch_release", fake_fetch)
    lines: list[str] = []
    code: int = upgrade_everything(say=lines.append)
    assert code == 0, "\n".join(lines)
    assert calls == ["latest"]
    expected: str = (ROOT / "core" / "VERSION").read_text().strip()
    for repo in repos:
        assert (repo / ".factory" / "VERSION").read_text().strip() == expected
    assert "4 upgraded, 0 current" in lines


def test_upgrade_dry_run_writes_nothing(world):
    installed = world.cli("install", "--tool", "cursor")
    assert installed.returncode == 0, installed.stderr
    (world.repo / ".factory" / "VERSION").write_text("0.0.0\n")
    home: Path = _global_home()
    result = world.cli("upgrade", "--dry-run", "--source", str(ROOT), cwd=world.tmp)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (world.repo / ".factory" / "VERSION").read_text().strip() == "0.0.0"
    assert (home / "core" / "VERSION").read_text().strip() == "0.0.0"
    assert "0.0.0 ->" in result.stdout
    assert "ok    .factory/" not in result.stdout
    assert "ok    global install" not in result.stdout


RESTART_NOTE: str = "still runs the old version"


def _user_files(home: Path, pid: int, url: str = "http://127.0.0.1:5999/") -> dict[str, str]:
    files: dict[str, str] = {
        "workspace.toml": "schema = 1\nprojects = []\n",
        "dashboard.json": json.dumps({"pid": pid, "port": 5999, "url": url}) + "\n",
        "dashboard.log": "serving\n",
        "plan-usage.json": '{"limit": 1}\n',
    }
    for name, text in files.items():
        (home / name).write_text(text)
    return files


def _sleeper() -> subprocess.Popen:
    """A live pid for dashboard.json. A thread reaps it, so it does not linger as a zombie once stopped."""
    child: subprocess.Popen = subprocess.Popen(["sleep", "60"], start_new_session=True)
    threading.Thread(target=child.wait, daemon=True).start()
    return child


def test_upgrade_self_keeps_the_user_files_and_replaces_the_release(world):
    home: Path = _global_home()
    child: subprocess.Popen = _sleeper()
    try:
        files: dict[str, str] = _user_files(home, child.pid)
        (home / "old-layout").mkdir()
        (home / "old-layout" / "x").write_text("old\n")
        (home / "core" / "stale.py").write_text("old\n")
        result = world.cli("upgrade", "--self", "--source", str(ROOT), cwd=world.tmp)
        assert result.returncode == 0, result.stderr + result.stdout
        for name, text in files.items():
            assert (home / name).read_text() == text, name
        assert (home / "core" / "VERSION").read_text() == (ROOT / "core" / "VERSION").read_text()
        assert not home.with_name(home.name + ".previous").exists()
        assert not (home / "old-layout").exists()
        assert not (home / "core" / "stale.py").exists()
    finally:
        child.kill()


def test_upgrade_says_a_running_dashboard_is_on_the_old_version_and_stop_still_works(world):
    home: Path = _global_home()
    child: subprocess.Popen = _sleeper()
    url: str = "http://127.0.0.1:5999/"
    try:
        _user_files(home, child.pid, url)
        result = world.cli("upgrade", "--self", "--source", str(ROOT), cwd=world.tmp)
        assert result.returncode == 0, result.stderr + result.stdout
        lines: list[str] = [line for line in result.stdout.splitlines() if line.strip()]
        assert lines[-1] == (
            f"note  dashboard at {url} still runs the old version. "
            "Restart it: mfactory stop dashboard && mfactory start dashboard"
        )
        assert child.poll() is None
        stopped = world.cli("stop", cwd=world.tmp)
        assert stopped.returncode == 0, stopped.stderr + stopped.stdout
        assert f"stopped {url}" in stopped.stdout
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()


def test_upgrade_prints_no_restart_note_without_a_live_dashboard(world):
    home: Path = _global_home()
    missing = world.cli("upgrade", "--self", "--source", str(ROOT), cwd=world.tmp)
    assert missing.returncode == 0, missing.stderr + missing.stdout
    assert "ok    global install" in missing.stdout
    assert RESTART_NOTE not in missing.stdout
    (home / "core" / "VERSION").write_text("0.0.0\n")
    dead = subprocess.Popen(["true"])
    dead.wait()
    _user_files(home, dead.pid)
    stale = world.cli("upgrade", "--self", "--source", str(ROOT), cwd=world.tmp)
    assert stale.returncode == 0, stale.stderr + stale.stdout
    assert "ok    global install" in stale.stdout
    assert RESTART_NOTE not in stale.stdout


def test_upgrade_dry_run_keeps_the_dashboard_files_and_prints_no_note(world):
    home: Path = _global_home()
    child: subprocess.Popen = _sleeper()
    try:
        files: dict[str, str] = _user_files(home, child.pid)
        result = world.cli("upgrade", "--dry-run", "--source", str(ROOT), cwd=world.tmp)
        assert result.returncode == 0, result.stderr + result.stdout
        assert (home / "core" / "VERSION").read_text() == "0.0.0\n"
        assert (home / "dashboard.json").read_text() == files["dashboard.json"]
        assert (home / "plan-usage.json").read_text() == files["plan-usage.json"]
        assert not home.with_name(home.name + ".previous").exists()
        assert RESTART_NOTE not in result.stdout
    finally:
        child.kill()


def test_upgrade_skips_a_project_whose_schema_differs(world):
    installed = world.cli("install", "--tool", "cursor")
    assert installed.returncode == 0, installed.stderr
    other: Path = world.tmp / "other"
    shutil.copytree(world.repo, other)
    added = world.cli("workspace", "add", other)
    assert added.returncode == 0, added.stderr
    for repo in (world.repo, other):
        (repo / ".factory" / "VERSION").write_text("0.0.0\n")
    config_path: Path = other / "factory.toml"
    config_path.write_text(config_path.read_text().replace("schema = 1", "schema = 99", 1))
    expected: str = (ROOT / "core" / "VERSION").read_text()
    _global_home(expected)
    result = world.cli("upgrade", "--source", str(ROOT), cwd=world.tmp)
    assert result.returncode == 1, result.stderr + result.stdout
    assert "schema 99, release needs 1" in result.stdout
    assert "set schema = 1" in result.stdout
    assert (other / ".factory" / "VERSION").read_text().strip() == "0.0.0"
    assert (world.repo / ".factory" / "VERSION").read_text().strip() == expected.strip()
    assert "1 upgraded, 1 current, 1 skipped" in result.stdout


def test_upgrade_leaves_a_current_project_in_place(world):
    installed = world.cli("install", "--tool", "cursor")
    assert installed.returncode == 0, installed.stderr
    expected: str = (ROOT / "core" / "VERSION").read_text()
    (world.repo / ".factory" / "VERSION").write_text(expected)
    _global_home(expected)
    binary: Path = world.repo / ".factory" / "bin" / "mfactory"
    before: int = binary.stat().st_mtime_ns
    result = world.cli("upgrade", "--source", str(ROOT), cwd=world.tmp)
    assert result.returncode == 0, result.stderr + result.stdout
    assert binary.stat().st_mtime_ns == before
    assert "already" in result.stdout
    assert "0 upgraded, 2 current" in result.stdout


def test_upgrade_repairs_a_project_missing_templates(world):
    # A project on the release's version whose .factory/ has no templates folder, as a 0.3.x CLI left it.
    installed = world.cli("install", "--tool", "cursor")
    assert installed.returncode == 0, installed.stderr
    expected: str = (ROOT / "core" / "VERSION").read_text()
    (world.repo / ".factory" / "VERSION").write_text(expected)
    shutil.rmtree(world.repo / ".factory" / "templates")
    _global_home(expected)
    result = world.cli("upgrade", "--source", str(ROOT), cwd=world.tmp)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "repair, missing: templates" in result.stdout
    assert "0 upgraded, 1 repaired, 1 current" in result.stdout
    assert "commit .factory/ and the generated files" in result.stdout
    assert (world.repo / ".factory" / "templates" / "factory.toml").is_file()
    for item in install.ITEMS + ["adapters"]:
        assert (world.repo / ".factory" / item).exists(), item


def test_status_all_points_at_upgrade_when_a_project_is_behind(world, monkeypatch):
    installed = world.cli("install", "--tool", "cursor")
    assert installed.returncode == 0, installed.stderr
    (world.repo / ".factory" / "VERSION").write_text("0.0.0\n")
    monkeypatch.chdir(world.tmp)
    monkeypatch.setattr("modfactory.cli.maintain.latest_release", lambda: "9.9.9")
    lines: list[str] = []
    assert status_all(say=lines.append) == 0
    assert lines[-1] == "run `mfactory upgrade`"
    monkeypatch.setattr("modfactory.cli.maintain.latest_release", lambda: "0.0.0")
    current: list[str] = []
    assert status_all(say=current.append) == 0
    assert "run `mfactory upgrade`" not in current


def test_changelog_note_is_one_version(tmp_path: Path) -> None:
    root: Path = tmp_path / "factory"
    (root / "core" / "bin").mkdir(parents=True)
    (root / "core" / "bin" / "mfactory").write_text("")
    bullets: list[str] = [f"- line {number}" for number in range(15)]
    (root / "CHANGELOG.md").write_text("## 1.2.3\n\n" + "\n".join(bullets) + "\n\n## 1.2.2\n\n- old\n")
    note: str = _changelog_note(root, "1.2.3")
    assert note.splitlines() == bullets[:12]
    assert _changelog_note(root, "9.9.9") == ""


def test_install_is_another_name_for_init(bare):
    res = bare.cli("install", "--tool", "cursor")
    assert res.returncode == 0, res.stderr
    assert 'enabled = ["cursor"]' in (bare.repo / "factory.toml").read_text()
    assert (bare.repo / ".factory/bin/mfactory").is_file()


def test_uninstall_keeps_factory_toml_unless_asked(world):
    world.cli("init")
    removed = world.cli("uninstall")
    assert removed.returncode == 0, removed.stderr
    assert "remove  .factory/" in removed.stdout
    assert not (world.repo / ".factory").exists()
    assert (world.repo / "factory.toml").is_file()
    assert "removed from" in removed.stdout
    world.cli("init")
    labelled = world.cli("uninstall", "--config", "--labels")
    assert labelled.returncode == 0, labelled.stderr
    assert not (world.repo / "factory.toml").exists()
    assert world.gh["labels"] == []


def test_install_sh_global_links_a_command_that_can_install(bare, tmp_path):
    home = tmp_path / "home"
    data = tmp_path / "data"
    bindir = tmp_path / "bin"
    env = dict(os.environ, HOME=str(home), FACTORY_HOME=str(data), FACTORY_BIN_DIR=str(bindir))
    res = subprocess.run(["bash", str(ROOT / "install.sh"), "--global", "--source", str(ROOT)],
                         cwd=tmp_path, capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    assert (data / ".global").is_file()
    assert (bindir / "mfactory").is_symlink()
    refused = subprocess.run(["bash", str(ROOT / "install.sh"), "--global", "--tool", "claude", "--source", str(ROOT)],
                             cwd=tmp_path, capture_output=True, text=True, env=env)
    assert refused.returncode == 1 and "--global" in refused.stderr
    kept: dict[str, str] = {
        "dashboard.json": '{"pid": 1, "port": 5999, "url": "http://127.0.0.1:5999/"}\n',
        "dashboard.log": "serving\n",
        "plan-usage.json": '{"limit": 1}\n',
    }
    for name, text in kept.items():
        (data / name).write_text(text)
    (data / "fleet.toml").write_text("schema = 1\nprojects = []\n")
    again = subprocess.run(["bash", str(ROOT / "install.sh"), "--global", "--source", str(ROOT)],
                           cwd=tmp_path, capture_output=True, text=True, env=env)
    assert again.returncode == 0, again.stderr
    for name, text in kept.items():
        assert (data / name).read_text() == text, name
    assert (data / "workspace.toml").read_text() == "schema = 1\nprojects = []\n"
    assert (data / ".global").is_file()
    assert (data / "core/bin/mfactory").is_file()
    installed = subprocess.run([sys.executable, str(data / "core/bin/mfactory"), "install", "--tool", "cursor"],
                               cwd=bare.repo, capture_output=True, text=True)
    assert installed.returncode == 0, installed.stderr
    assert 'enabled = ["cursor"]' in (bare.repo / "factory.toml").read_text()
