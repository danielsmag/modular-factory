"""The command line, and installing, checking and upgrading the factory in a project."""

import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest
from conftest import ROOT, git
from modfactory.fleet.service import DashboardServer, one_handler
from modfactory.util import dashboard_page


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
                 "defaults.toml", "adapters/claude.toml", "web/dashboard.html", "web/dashboard.css",
                 "web/dashboard.js", "VERSION", "LICENSE",
                 "NOTICE"):
        assert (repo / ".factory" / item).exists(), item
    assert not list((repo / ".factory").rglob("__pycache__"))
    assert installed(bare).stdout.strip() == (ROOT / "core/VERSION").read_text().strip()

    toml = (repo / "factory.toml").read_text()
    assert 'enabled = ["claude", "codex"]' in toml and 'base_branch = "main"' in toml
    assert (repo / ".gitignore").read_text() == (
        "node_modules/\n\n# software factory runtime state\n/.factory/jobs/\n/.worktrees/\n")
    assert (repo / ".claude/agents/mfactory-build.md").exists()
    assert (repo / ".codex/agents/mfactory-build.toml").exists()
    assert not (repo / ".cursor").exists()
    assert bare.gh["labels"] == ["factory", "factory:needs-human", "factory:needs-info"]
    assert "Next:" in res.stdout and "/mfactory setup" in res.stdout


def test_init_again_keeps_factory_toml_and_changes_nothing_else(bare):
    bare.cli("init")
    (bare.repo / "factory.toml").write_text('schema = 1\n[run]\nconcurrency = 7\n')
    before = (bare.repo / ".gitignore").read_text()
    res = bare.cli("init", "--tool", "cursor")
    assert res.returncode == 0
    assert "factory.toml exists and was left as it is" in res.stdout
    assert (bare.repo / "factory.toml").read_text() == 'schema = 1\n[run]\nconcurrency = 7\n'
    assert (bare.repo / ".gitignore").read_text() == before


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
    assert "001-add-search  #1     spec" in world.cli("summary").stdout


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
    assert world.cli("sync").stdout == "create  .claude/agents/mfactory-debug.md\n"
    assert world.cli("sync", "--check").returncode == 0


def test_models_and_graph_describe_the_setup(world):
    world.configure('[tools]\nenabled = ["claude", "codex"]\n[steps.debug]\nenabled = true\n')
    models = world.cli("models").stdout.splitlines()
    assert models[0].split() == ["agent", "claude", "codex"]
    assert "mfactory-debug              opus / high      inherit / high" in models
    graph = world.cli("graph").stdout
    assert "gate           checks  PASS -> review | FAIL -> debug (max 2)" in graph
    assert "spec-approval  human   off, continues as APPROVE -> build" in graph


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
    for line in ("ok    gh is signed in", "ok    factory.toml is valid: 9 steps on, off: spec-approval, debug, verify",
                 "ok    generated agent and skill files match factory.toml", "ok    origin/main can be fetched",
                 "ok    .factory/jobs/ is ignored by git", "ok    GitHub labels exist",
                 "Everything a run needs is in place."):
        assert line in res.stdout, line
    assert "ok    the checks pass on origin/main" in res.stdout
    assert "warn  Claude Code: `claude` isn't on PATH" in res.stdout


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


def test_doctor_warns_when_the_factory_is_not_on_the_base_branch(bare):
    bare.cli("init")
    (bare.repo / "factory.toml").write_text(
        'schema = 1\n[project]\nbase_branch = "main"\n[checks]\ncommands = ["true"]\n')
    res = doctor(bare)
    assert "warn  factory.toml and .factory/ aren't on origin/main yet." in res.stdout


# ---- upgrade ---------------------------------------------------------------


def newer(world, version="9.9.9", schema=1):
    """A copy of the factory's source that claims to be a later version."""
    copy = world.tmp / "newer"
    shutil.copytree(ROOT / "core", copy / "core", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "adapters", copy / "adapters")
    shutil.copytree(ROOT / "web", copy / "web")
    for name in ("LICENSE", "NOTICE"):
        shutil.copy(ROOT / name, copy / name)
    (copy / "core/VERSION").write_text(version + "\n")
    defaults = copy / "core/defaults.toml"
    defaults.write_text(defaults.read_text().replace("schema = 1", f"schema = {schema}"))
    role = copy / "core/roles/build.md"
    role.write_text(role.read_text() + "\nNew in this version.\n")
    return copy


def test_upgrade_replaces_the_core_and_leaves_factory_toml_and_jobs(world):
    cli = ready(world)
    job = world.start(until="spec")
    world.factory()
    toml = (world.repo / "factory.toml").read_text()
    (world.repo / ".factory/dashboard.html").write_text("old page\n")
    res = subprocess.run([str(cli), "upgrade", "--source", str(newer(world))], cwd=world.repo,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "ok    .factory/ 0.1.0 -> 9.9.9" in res.stdout
    assert installed(world).stdout.strip() == "9.9.9"
    assert "New in this version." in (world.repo / ".factory/roles/build.md").read_text()
    assert (world.repo / ".factory/web/dashboard.html").is_file()
    assert not (world.repo / ".factory/dashboard.html").exists()
    assert (world.repo / "factory.toml").read_text() == toml
    assert (world.repo / ".factory/jobs" / job / "job.json").exists()


def test_upgrade_refuses_a_schema_it_cannot_migrate(world):
    cli = ready(world)
    res = subprocess.run([str(cli), "upgrade", "--source", str(newer(world, schema=2))], cwd=world.repo,
                         capture_output=True, text=True)
    assert res.returncode == 1
    assert "version 9.9.9 reads schema 2, and factory.toml is schema 1" in res.stderr
    assert installed(world).stdout.strip() == "0.1.0"


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
    for remote in ("<script src", "<link", "@import", "url(http", 'src="http', "<iframe", "<img"):
        assert remote not in page, remote


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to parse the page's script")
def test_the_dashboard_script_parses(tmp_path):
    page = dashboard_page().decode()
    script = page[page.index("<script>") + len("<script>"):page.rindex("</script>")]
    (tmp_path / "page.js").write_text(script)
    result = subprocess.run(["node", "--check", str(tmp_path / "page.js")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_is_another_name_for_init(bare):
    res = bare.cli("install", "--tool", "cursor")
    assert res.returncode == 0, res.stderr
    assert 'enabled = ["cursor"]' in (bare.repo / "factory.toml").read_text()
    assert (bare.repo / ".factory/bin/mfactory").is_file()


def test_status_reports_version_tools_and_stale_files(world):
    world.cli("init")
    fresh = world.cli("status")
    assert fresh.returncode == 0, fresh.stderr
    assert "project 0.1.0" in fresh.stdout
    assert "sync    up to date" in fresh.stdout
    (world.repo / ".claude/agents/mfactory-build.md").write_text("hand edit\n")
    stale = world.cli("status")
    assert "sync    1 file(s) out of date" in stale.stdout


def test_uninstall_keeps_factory_toml_unless_asked(world):
    world.cli("init")
    assert world.cli("fleet", "subscribe").returncode == 0
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
    installed = subprocess.run([sys.executable, str(data / "core/bin/mfactory"), "install", "--tool", "cursor"],
                               cwd=bare.repo, capture_output=True, text=True)
    assert installed.returncode == 0, installed.stderr
    assert 'enabled = ["cursor"]' in (bare.repo / "factory.toml").read_text()


def test_upgrade_self_refuses_a_project_and_replaces_a_global_tree(world, tmp_path):
    refused = subprocess.run([sys.executable, str(ROOT / "core/bin/mfactory"), "upgrade", "--self"],
                             capture_output=True, text=True)
    assert refused.returncode == 1 and "not the global install" in refused.stderr
    world.cli("init")
    project = subprocess.run([sys.executable, str(world.repo / ".factory/bin/mfactory"), "upgrade", "--self"],
                             cwd=world.repo, capture_output=True, text=True)
    assert project.returncode == 1 and "not the global install" in project.stderr

    home = tmp_path / "global"
    shutil.copytree(ROOT / "core", home / "core", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "adapters", home / "adapters")
    for name in ("LICENSE", "NOTICE"):
        shutil.copy(ROOT / name, home / name)
    (home / ".global").write_text(f"{home}\n")
    res = subprocess.run([sys.executable, str(home / "core/bin/mfactory"), "upgrade", "--self",
                          "--source", str(newer(world))], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "0.1.0 -> 9.9.9" in res.stdout
    assert (home / "core/VERSION").read_text().strip() == "9.9.9"
    assert (home / ".global").is_file()


def test_upgrade_all_reports_a_failure_and_still_upgrades_the_rest(world, tmp_path):
    world.cli("init")
    missing = tmp_path / "missing"
    missing.mkdir()
    from modfactory.fleet import Fleet, Project, user_file
    file = user_file()
    file.parent.mkdir(parents=True, exist_ok=True)
    group = Fleet(file, [])
    group.projects = [Project(world.repo.resolve()), Project(missing.resolve())]
    group.save()
    res = world.cli("upgrade", "--all", "--source", str(newer(world, version="8.8.8")))
    assert res.returncode == 1, res.stderr
    assert "0.1.0 -> 8.8.8" in res.stdout
    assert "FAIL" in res.stdout
    assert (world.repo / ".factory/VERSION").read_text().strip() == "8.8.8"
