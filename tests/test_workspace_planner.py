"""Collecting the planner snapshot across every project in the workspace."""

import json
import shutil
import stat
import sys
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from conftest import AUTO_MERGE, CLI, World, git
from modfactory.planner.state import PlannerState
from modfactory.workspace import Workspace
from modfactory.workspace import planner as workspace_planner
from modfactory.workspace.paths import SCHEMA


def launcher(repo, state_file=None):
    """`.factory/bin/mfactory` for a test checkout: the repo's own CLI, with its own stub `gh` state."""
    binary = repo / ".factory" / "bin" / "mfactory"
    binary.parent.mkdir(parents=True, exist_ok=True)
    env = f"GH_STATE={state_file} " if state_file else ""
    binary.write_text(f'#!/bin/sh\n{env}exec {sys.executable} {CLI} "$@"\n')
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


class Pair:
    """Two checkouts, `repo` and `beta`, each with its own stub tracker, in one workspace file."""

    def __init__(self, world, tmp_path, monkeypatch):
        self.a = world
        self.monkeypatch = monkeypatch
        self.b = World(tmp_path / "b")
        self.b.repo = tmp_path / "beta"
        self.b.state_file = tmp_path / "gh-beta.json"
        self.b.state_file.write_text(world.state_file.read_text())
        self.b.origin = world.origin
        shutil.copytree(world.repo, self.b.repo, symlinks=True)
        git("remote", "set-url", "origin", str(world.origin), cwd=self.b.repo)
        launcher(world.repo)
        launcher(self.b.repo, self.b.state_file)
        self.file = tmp_path / "workspace.toml"
        self.file.write_text(f'schema = {SCHEMA}\n\n[[projects]]\npath = "{world.repo}"\n\n'
                             f'[[projects]]\npath = "{self.b.repo}"\n')
        monkeypatch.setenv("FACTORY_WORKSPACE", str(self.file))
        self.state = PlannerState(tmp_path / "planner-state")

    @contextmanager
    def in_b(self):
        self.monkeypatch.setenv("GH_STATE", str(self.b.state_file))
        try:
            yield self.b
        finally:
            self.monkeypatch.setenv("GH_STATE", str(self.a.state_file))

    def collect(self, settings=None):
        return workspace_planner.collect(Workspace.load(self.file), settings or {"max_pairs": 40}, self.state)


@pytest.fixture
def pair(world, tmp_path, monkeypatch):
    return Pair(world, tmp_path, monkeypatch)


def test_collect_merges_two_projects_and_pairs_across_them(pair):
    pair.a.issue("Add VAT field to the invoice API")
    with pair.in_b() as b:
        b.issue("Show VAT field from invoice API")
    snap = pair.collect()
    assert snap["schema"] == 1 and snap["projects"] == ["repo", "beta"] and snap["errors"] == []
    assert sorted(t["ref"] for t in snap["tickets"]) == ["beta:#1", "repo:#1"]
    assert [(p["a"].split(":")[0], p["b"].split(":")[0]) for p in snap["candidates"]] in (
        [("repo", "beta")], [("beta", "repo")])
    assert pair.state.read_snapshot() == snap
    assert snap["released"] == []


def test_collect_tolerates_old_core(pair):
    pair.a.issue("Add VAT field to the invoice API")
    old = pair.b.repo / ".factory" / "bin" / "mfactory"
    old.write_text("#!/bin/sh\necho \"mfactory: error: invalid choice: 'plan'\" >&2\nexit 2\n")
    snap = pair.collect()
    assert [t["ref"] for t in snap["tickets"]] == ["repo:#1"]
    assert snap["errors"] == ["beta: its factory is too old for the planner; run mfactory upgrade"]


def test_collect_hash_changes_only_when_tickets_change(pair):
    pair.a.issue("Add VAT field to the invoice API")
    first = pair.collect()["hash"]
    assert pair.collect()["hash"] == first
    pair.a.set_gh(lambda state: state["issues"]["1"].update(title="Add the VAT field"))
    assert pair.collect()["hash"] != first


def test_collect_hash_changes_when_a_stage_changes(pair):
    job = pair.a.create(pair.a.issue("Add VAT field to the invoice API"))
    first = pair.collect()["hash"]
    assert pair.collect()["hash"] == first
    pair.a.play(job, until="spec")
    assert pair.collect()["hash"] != first


def test_collect_times_out_a_hung_project(pair, monkeypatch):
    pair.a.issue("Add VAT field to the invoice API")
    (pair.b.repo / ".factory" / "bin" / "mfactory").write_text("#!/bin/sh\nsleep 30\n")
    monkeypatch.setattr(workspace_planner, "SNAPSHOT_TIMEOUT", 1)
    snap = pair.collect()
    assert [t["ref"] for t in snap["tickets"]] == ["repo:#1"]
    assert snap["errors"] == ["beta: timed out"]


def test_collect_survives_a_binary_that_cannot_run(pair):
    pair.a.issue("Add VAT field to the invoice API")
    (pair.b.repo / ".factory" / "bin" / "mfactory").chmod(0)
    snap = pair.collect()
    assert [t["ref"] for t in snap["tickets"]] == ["repo:#1"]
    assert len(snap["errors"]) == 1 and snap["errors"][0].startswith("beta:")


def test_collect_releases_cross_project_hold(pair):
    first = pair.a.start(until="done", config_text=AUTO_MERGE, title="Add the tax field")
    with pair.in_b() as b:
        job = b.create(b.issue("Show the tax field"))
        held = b.dir(job) / "job.json"
        data = json.loads(held.read_text())
        data["held_by"] = {"until": "repo:#1", "project": str(pair.a.repo), "key": 1, "reason": "needs the API",
                           "since": datetime.now(UTC).isoformat()}
        held.write_text(json.dumps(data))
        b.factory().store.release()  # the CLI that releases the hold takes the job's lock itself
    assert pair.a.job(first)["step"] == "done"
    snap = pair.collect()
    assert snap["released"] == ["beta:#1"]
    assert "held_by" not in json.loads(held.read_text())


def test_collect_cli_prints_counts(pair):
    pair.a.issue("Add VAT field to the invoice API")
    with pair.in_b() as b:
        b.issue("Show VAT field from invoice API")
    res = pair.a.cli("plan", "collect")
    assert res.returncode == 0, res.stderr
    assert "2 tickets" in res.stdout and "1 pair" in res.stdout
    out = json.loads(pair.a.cli("plan", "collect", "--json").stdout)
    assert len(out["tickets"]) == 2 and out["errors"] == []
