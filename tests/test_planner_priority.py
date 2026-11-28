"""The [planner] settings and how a ticket's priority becomes one of four levels."""

import pytest
from modfactory import config
from modfactory.planner import priority
from modfactory.service import tracker as trackers
from modfactory.service.gh import GitHub
from modfactory.util import ConfigError


def problems(world, text):
    world.configure(text)
    with pytest.raises(ConfigError) as error:
        config.load(world.repo)
    return error.value.problems


def test_label_levels_default_and_override():
    assert priority.label_level(["bug", "P1"], {}) == "high"
    assert priority.label_level(["Priority:Critical", "P3"], {}) == "critical"
    assert priority.label_level(["urgent"], {"urgent": "critical"}) == "critical"
    assert priority.label_level([], {}) == "normal"


def test_jira_levels():
    assert priority.jira_level("Highest", {}) == "critical"
    assert priority.jira_level("Medium", {}) == "normal"
    assert priority.jira_level(None, {}) == "normal"
    assert priority.jira_level("P2", {"P2": "high"}) == "high"


def test_github_project_field(world):
    world.set_gh(lambda state: state.setdefault("projects", {}).update(
        {"acme/3": {"items": [{"content": {"number": 12}, "priority": "P0"}]}}))
    gh = GitHub(world.repo)
    assert priority.github_project_levels(gh, "acme", 3, "Priority") == {12: "P0"}
    settings = {"priority": {"labels": {}, "jira": {}, "github_project": {}}}
    assert priority.level_for("github", {"number": 12, "labels": []}, settings, {12: "P0"}) == "critical"
    assert priority.level_for("github", {"number": 13, "labels": [{"name": "P1"}]}, settings, {12: "P0"}) == "high"
    assert priority.level_for("jira", {"number": "A-1", "priority": "Low", "labels": []}, settings, {}) == "low"
    assert priority.level_for("jira", {"number": "A-1", "priority": None, "labels": []}, settings, {}) == "normal"


def test_github_project_failure_is_empty(world):
    world.set_gh(lambda state: state.update(fail="project item-list"))
    assert priority.github_project_levels(GitHub(world.repo), "acme", 3, "Priority") == {}


def test_planner_settings_are_checked(world):
    world.configure("")
    assert config.load(world.repo).data["planner"]["frequency"] == "medium"
    assert any("[planner].frequency" in p for p in problems(world, '[planner]\nfrequency = "often"'))
    assert problems(world, '[planner]\nalert_at = "urgent"')
    assert problems(world, "[planner]\nmax_pairs = 0")
    assert problems(world, '[planner.priority.labels]\nP1 = "hot"')
    assert problems(world, '[planner.priority.github_project]\nowner = "acme"\nnumber = 0')


def test_jira_queue_issues_carry_priority(world):
    jira = world.use_jira()
    jira.ticket("Do it", "body", priority="High")
    assert trackers.make(config.load(world.repo)).queue()[0]["priority"] == "High"
