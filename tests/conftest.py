"""A project with a bare `origin`, a stub `gh`, and stand-ins for the agents."""

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core" / "lib"))
os.environ["FACTORY_VERIFY_POLL"] = "0.05"

import stub_jira  # noqa: E402
from modfactory import config, sync  # noqa: E402
from modfactory.engine import Factory  # noqa: E402

CLI = ROOT / "core" / "bin" / "mfactory"


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path, monkeypatch):
    """Workspace commands and rule lookup must not read the developer's real home."""
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "mfactory"))
    home = tmp_path / "skill-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sync, "skill_home", lambda: home)


BASE_CONFIG = """\
schema = 1

[project]
description = "A test project"
base_branch = "main"
"""
# The checks fail when the branch has a file named FAIL.
BASE_CHECKS = """
[checks]
commands = ["test ! -f FAIL"]
"""
# Opt in to the script merging. The default waits for a person.
AUTO_MERGE = """
[steps.approve]
on = { APPROVE = "merge" }

[steps.ready-to-merge]
enabled = false
skip = "REJECT"

[steps.merge]
enabled = true
"""


# One Jira status per default step, as in the fake board of tests/stub_jira.py.
JIRA_COLUMNS = """
[tracker.jira.columns]
queue = "Ready for factory"
intake = "Intake"
ask-author = "Needs info"
spec = "Spec"
spec-approval = "Spec approval"
build = "Build"
gate = "Checks"
debug = "Debug"
review = "Review"
approve = "Approve"
ready-to-merge = "Ready to merge"
merge = "Merging"
ready-to-verify = "Ready to verify"
verify = "Verify"
escalated = "Escalated"
done = "Done"
rejected = "Rejected"
failed = "Failed"
"""


def git(*args, cwd):
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert res.returncode == 0, f"git {' '.join(args)}: {res.stderr}"
    return res.stdout.strip()


def _git(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    assert res.returncode == 0, f"git {' '.join(args)}: {res.stderr}"


@pytest.fixture(scope="session")
def repo_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """One committed origin and checkout. Each test copies it instead of running git init."""
    root: Path = tmp_path_factory.mktemp("template")
    env: dict[str, str] = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    })
    origin: Path = root / "origin.git"
    repo: Path = root / "repo"
    _git(["init", "-q", "--bare", "-b", "main", str(origin)], root, env)
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo, env)
    (repo / "README.md").write_text("# app\n")
    (repo / "AGENTS.md").write_text("# Agents\n")
    (repo / ".gitignore").write_text("/.factory/jobs/\n/.worktrees/\n")
    (repo / "factory.toml").write_text(BASE_CONFIG + BASE_CHECKS)
    _git(["add", "-A"], repo, env)
    _git(["commit", "-q", "-m", "init"], repo, env)
    _git(["remote", "add", "origin", str(origin)], repo, env)
    _git(["push", "-q", "-u", "origin", "main"], repo, env)
    return origin, repo


class World:
    def __init__(self, tmp):
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.origin = tmp / "origin.git"
        self.state_file = tmp / "gh.json"
        self._factory = None
        self.jira = None
        self.github_issues = False  # with both trackers, `issue()` makes GitHub issues and `jira.ticket()` cards
        self.acli_log = tmp / "acli.log"
        self.bin = tmp / "bin"
        self.monkeypatch = None

    # ---- Jira --------------------------------------------------------------

    def use_jira(self, extra="", columns=JIRA_COLUMNS, settings=""):
        """Make this project take its jobs from a fake Jira board. `world.jira` is that board."""
        if self.jira is None:
            self.jira = stub_jira.FakeJira()
        self.configure(f'[tracker]\nkind = "jira"\n\n[tracker.jira]\nsite = "{self.jira.url}"\nboard = 7\n'
                       f"{settings}\n{columns}\n{extra}")
        return self.jira

    def use_both(self, extra="", columns=JIRA_COLUMNS, settings="", kind='["github", "jira"]'):
        """Make this project take jobs from GitHub issues and the fake Jira board at once."""
        if self.jira is None:
            self.jira = stub_jira.FakeJira()
        self.github_issues = True
        self.configure(f'[tracker]\nkind = {kind}\n\n[tracker.jira]\nsite = "{self.jira.url}"\nboard = 7\n'
                       f"{settings}\n{columns}\n{extra}")
        return self.jira

    def use_acli(self, extra="", columns=JIRA_COLUMNS, settings='project = "PROJ"'):
        """As `use_jira`, but with no REST credentials and a stub `acli` on PATH, so the factory falls back to it."""
        jira = self.use_jira(extra=extra, columns=columns, settings=settings)
        shutil.copy(ROOT / "tests" / "stub_acli.py", self.bin / "acli")
        (self.bin / "acli").chmod(0o755)
        self.monkeypatch.delenv("JIRA_EMAIL", raising=False)
        self.monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        basic = base64.b64encode(f"{stub_jira.EMAIL}:{stub_jira.TOKEN}".encode()).decode()
        for name, value in {"ACLI_STUB_SITE": jira.url, "ACLI_STUB_AUTH": f"Basic {basic}",
                            "ACLI_STUB_LOG": str(self.acli_log)}.items():
            self.monkeypatch.setenv(name, value)
        return jira

    def acli_calls(self, *start):
        """The `acli` commands run so far whose words start with `start`."""
        if not self.acli_log.exists():
            return []
        calls = [json.loads(line) for line in self.acli_log.read_text().splitlines()]
        return [call for call in calls if call[:len(start)] == list(start)]

    # ---- GitHub ------------------------------------------------------------

    @property
    def gh(self):
        return json.loads(self.state_file.read_text())

    def set_gh(self, change):
        state = self.gh
        change(state)
        self.state_file.write_text(json.dumps(state))

    def issue(self, title="Add a thing", body="Please add it.", labels=("factory",), author="alice",
              assignees=()):
        if self.jira and not self.github_issues:
            return self.jira.ticket(title, body, reporter=author, assignee=next(iter(assignees), None),
                                    labels=[n for n in labels if n != "factory"])
        number = self.gh["next_number"] + 1

        def add(state):
            state["next_number"] = number
            state["issues"][str(number)] = {
                "number": number, "title": title, "body": body, "state": "OPEN", "author": {"login": author},
                "assignees": [{"login": login} for login in assignees],
                "labels": [{"name": n} for n in labels], "url": f"https://github.test/acme/app/issues/{number}",
                "comments": [], "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-02T00:00:00Z"}
        self.set_gh(add)
        return number

    def comment(self, number, body, user, kind="issues"):
        def add(state):
            state["next_comment"] += 1
            state[kind][str(number)]["comments"].append({
                "id": f"C{state['next_comment']}", "author": {"login": user}, "body": body,
                "createdAt": "2026-01-01T00:00:00Z"})
        self.set_gh(add)

    def comments(self, number, kind="issues"):
        return [c["body"] for c in self.gh[kind][str(number)]["comments"]]

    def labels(self, number):
        return [label["name"] for label in self.gh["issues"][str(number)]["labels"]]

    def calls(self, prefix):
        return [c for c in self.gh["calls"] if " ".join(c).startswith(prefix)]

    # ---- The factory -------------------------------------------------------

    def configure(self, extra):
        """Replace what this test adds to the base factory.toml."""
        checks = "" if "[checks]" in extra else BASE_CHECKS
        (self.repo / "factory.toml").write_text(BASE_CONFIG + checks + "\n" + extra)

    def factory(self):
        if self._factory:
            self._factory.store.release()
        self.said = []
        self._factory = Factory(config.load(self.repo), say=self.said.append)
        return self._factory

    def create(self, issue, pipeline=None):
        return self.factory().create(issue, pipeline)["id"]

    def pipeline_file(self, name, text):
        """Write `pipelines/<name>` in the project: one or more YAML pipelines."""
        folder = self.repo / "pipelines"
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(text)

    def next(self, job):
        self.factory().next(job)
        return self.said

    def done(self, job, reply=None):
        self.factory().done(job, reply)
        return self.said

    def answer(self, job, outcome, text=""):
        self.factory().answer(job, outcome, text)
        return self.said

    def queue(self):
        return self.factory().queue()

    def job(self, ref):
        factory = self._factory or self.factory()
        return factory.store.read(factory.store.find(ref))

    def dir(self, job):
        return self.repo / ".factory" / "jobs" / self.job(job)["id"]

    def cli(self, *args, input=None, cwd=None):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=cwd or self.repo, input=input,
                              capture_output=True, text=True)

    # ---- Stand-ins for the agents ------------------------------------------

    def write(self, job, rel, text):
        path = self.dir(job) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def intake(self, job, decision="ACCEPT", risk="low", surfaces="none"):
        self.write(job, "intake.md", f"DECISION: {decision}\nRISK: {risk}\nSURFACES: {surfaces}\nRESCOPED: no\n\n"
                                     "## Summary\n- decided\n\n## Scope\nBuild it.\n\n## Questions\n1. Which?\n")

    def spec(self, job):
        self.write(job, "spec.md", "STATUS: DONE\n\n# A thing\n\n## Acceptance criteria\n1. It exists.\n\n"
                                     "## Verify after deploy\n1. The feature file is on the base branch.\n")

    def build(self, job, files=None, status="READY", push=True, commit=True):
        j = self.job(job)
        tree = Path(j["worktree"])
        for name, text in (files if files is not None else {f"feature-{j['round']}-{j['attempt']}.txt": "x\n"}).items():
            if text is None:
                (tree / name).unlink()
            else:
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                (tree / name).write_text(text)
        if commit:
            git("add", "-A", cwd=tree)
            git("commit", "-q", "--allow-empty", "-m", f"round {j['round']}", cwd=tree)
        if push:
            git("push", "-q", "-u", "origin", j["branch"], cwd=tree)
            if not self.gh["prs"] or all(p["headRefName"] != j["branch"] for p in self.gh["prs"].values()):
                # A Jira key goes in the title too, so Jira links the pull request.
                named = isinstance(j["issue"], str)
                title = f"{j['title']} [{j['issue']}]" if named else j["title"]
                link = f"Refs {j['issue']}" if named else f"Refs #{j['issue']}"
                subprocess.run(["gh", "pr", "create", "--draft", "--base", "main", "--head", j["branch"],
                                "--title", title, "--body", link], cwd=tree, check=True,
                               capture_output=True)
        self.write(job, "build.md", f"STATUS: {status}\n\n## Summary\n- built\n\n## Blocked\nNeeds a decision.\n")

    def review(self, job, changes=()):
        j = self.job(job)
        for reviewer in j["agents"]["reviewers"]:
            verdict = "CHANGES" if reviewer in changes else "PASS"
            self.write(job, f"round-{j['round']}/review-{reviewer}.md", f"VERDICT: {verdict}\n\n## Findings\nNone.\n")

    def approve(self, job, decision="APPROVE"):
        self.write(job, "decision.md", f"DECISION: {decision}\n\n## Summary\n- decided\n\n## Why\nBecause.\n")

    def debug(self, job, outcome="FOUND"):
        j = self.job(job)
        self.write(job, f"round-{j['round']}/debug-{j['attempt']}.md",
                   f"DIAGNOSIS: {outcome}\n\n## Cause\nA file named FAIL.\n\n## Fix\nRemove it.\n")

    def verifier(self, job, outcome="PASS"):
        self.write(job, "verify.md", f"VERIFY: {outcome}\n\n## Summary\n- checked\n\n## Failures\n1. It is missing.\n")

    def act(self, job):
        """Do what the agents of the job's current step would do, all of them succeeding."""
        j = self.job(job)
        cfg = self._factory.cfg.pipeline_of(j)
        step = cfg.steps[j["step"]]
        role = cfg.role(j["step"]) if step["type"] == "agent" else step["type"]
        {"intake": self.intake, "spec": self.spec, "build": self.build, "review": self.review,
         "approve": self.approve, "debug": self.debug, "verify": self.verifier}[role](job)

    def play(self, job, until=None, limit=40):
        """Run a job like the orchestrator would, until it reaches `until`, waits or finishes."""
        lines = []
        for _ in range(limit):
            if self.job(job)["step"] == until:
                return lines
            said = list(self.next(job))
            lines += said
            if self.job(job)["step"] == until or not any(s.startswith("start-agent") for s in said):
                return lines
            self.act(job)
            lines += self.done(job)
        raise AssertionError(f"{job} did not settle: {lines}")

    def start(self, until=None, config_text=None, **issue):
        if config_text is not None:
            self.configure(config_text)
        job = self.create(self.issue(**issue))
        self.play(job, until)
        return job


@pytest.fixture
def world(tmp_path, monkeypatch, repo_template):
    w = World(tmp_path)
    w.monkeypatch = monkeypatch
    stubs = w.bin
    stubs.mkdir()
    shutil.copy(ROOT / "tests" / "stub_gh.py", stubs / "gh")
    (stubs / "gh").chmod(0o755)
    for key, value in {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GH_STATE": str(w.state_file), "GH_ORIGIN": str(w.origin),
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com", "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "JIRA_EMAIL": stub_jira.EMAIL, "JIRA_API_TOKEN": stub_jira.TOKEN,
    }.items():
        monkeypatch.setenv(key, value)
    w.state_file.write_text(json.dumps({
        "user": "operator", "permissions": {"maintainer": "write"}, "labels": [], "issues": {}, "prs": {},
        "next_number": 0, "next_comment": 0, "calls": [], "fail": None}))

    origin: Path
    repo: Path
    origin, repo = repo_template
    shutil.copytree(origin, w.origin, symlinks=True)
    shutil.copytree(repo, w.repo, symlinks=True)
    git("remote", "set-url", "origin", str(w.origin), cwd=w.repo)
    yield w
    if w._factory:
        w._factory.store.release()
    if w.jira:
        w.jira.stop()
