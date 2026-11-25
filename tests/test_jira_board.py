"""`[tracker.jira]`: the settings, and the table that maps each step to a Jira status."""

import pytest
from modfactory import board, config
from modfactory.util import ConfigError

JIRA = """
[tracker]
kind = "jira"

[tracker.jira]
site = "https://acme.atlassian.net"
board = 7
"""
COLUMNS = """
[tracker.jira.columns]
queue = "Ready for factory"
intake = "Intake"
build = "Build"
gate = "Build"
done = "Done"
rejected = "Rejected"
"""


def problems(world, text):
    world.configure(text)
    with pytest.raises(ConfigError) as caught:
        config.load(world.repo)
    return "\n".join(caught.value.problems)


def columns(world, text=JIRA + COLUMNS):
    world.configure(text)
    return config.load(world.repo)["tracker"]["jira"]["columns"]


# ---- Settings -------------------------------------------------------------------


def test_a_jira_tracker_with_a_site_a_board_and_a_queue_column_is_valid(world):
    world.configure(JIRA + COLUMNS)
    assert config.load(world.repo)["tracker"]["kind"] == "jira"


def test_jira_needs_a_site_and_a_board(world):
    found = problems(world, '[tracker]\nkind = "jira"\n' + COLUMNS)
    assert "[tracker.jira].site is missing" in found
    assert "[tracker.jira].board is missing" in found


def test_the_site_must_be_https(world):
    found = problems(world, JIRA.replace("https://acme.atlassian.net", "http://acme.atlassian.net") + COLUMNS)
    assert "[tracker.jira].site must be an https:// address" in found


def test_a_local_site_may_be_plain_http(world):
    world.configure(JIRA.replace("https://acme.atlassian.net", "http://127.0.0.1:8123") + COLUMNS)
    assert config.load(world.repo)["tracker"]["jira"]["site"] == "http://127.0.0.1:8123"


def test_credentials_are_named_by_environment_variable_and_never_given(world):
    world.configure(JIRA + COLUMNS)
    jira = config.load(world.repo)["tracker"]["jira"]
    assert (jira["email_env"], jira["token_env"]) == ("JIRA_EMAIL", "JIRA_API_TOKEN")
    found = problems(world, JIRA + 'token = "secret"\n' + COLUMNS)
    assert "[tracker.jira].token is not a setting" in found


def test_an_unknown_tracker_setting_is_named(world):
    found = problems(world, '[tracker]\nkind = "github"\nboard = 7\n')
    assert "[tracker].board is not a setting" in found


def test_jira_cannot_close_issues_from_a_pull_request(world):
    world.configure(JIRA + COLUMNS)
    path = world.repo / "factory.toml"
    path.write_text(path.read_text().replace("[project]\n", '[project]\nissue_link = "closes"\n', 1))
    with pytest.raises(ConfigError, match=r'issue_link = "closes"'):
        config.load(world.repo)


# ---- The columns table ----------------------------------------------------------


def test_the_queue_column_is_required(world):
    found = problems(world, JIRA + COLUMNS.replace('queue = "Ready for factory"\n', ""))
    assert "[tracker.jira.columns].queue is missing" in found


def test_a_column_key_that_is_not_a_step_is_named_with_the_steps(world):
    found = problems(world, JIRA + COLUMNS + 'biuld = "Build"\n')
    assert "[tracker.jira.columns].biuld is not a step" in found
    assert "build" in found


def test_a_custom_step_can_have_a_column(world):
    custom = """
[steps.approve]
on = { APPROVE = "lint-docs", ESCALATE = "escalated" }

[steps.lint-docs]
type = "agent"
instructions = "Check the docs."
on = { DONE = "ready-to-merge" }
"""
    found = columns(world, JIRA + COLUMNS + 'lint-docs = "Docs"\n' + custom)
    assert board.status_for(found, "lint-docs") == "Docs"


def test_a_status_is_a_name_and_only_the_queue_may_list_several(world):
    assert "[tracker.jira.columns].build must be a status name" in problems(
        world, JIRA + COLUMNS.replace('build = "Build"', 'build = ["Build", "Doing"]'))
    found = columns(world, JIRA + COLUMNS.replace('queue = "Ready for factory"', 'queue = ["Ready", "To do"]'))
    assert board.queue_statuses(found) == ["Ready", "To do"]


# ---- Reading the mapping --------------------------------------------------------


def test_a_step_maps_to_its_status_and_steps_can_share_one(world):
    found = columns(world)
    assert board.status_for(found, "intake") == "Intake"
    assert board.status_for(found, "build") == board.status_for(found, "gate") == "Build"
    assert board.status_for(found, "done") == "Done"


def test_an_unmapped_step_has_no_status(world):
    assert board.status_for(columns(world), "review") is None


def test_the_queue_is_never_a_steps_status(world):
    found = columns(world)
    assert board.queue_statuses(found) == ["Ready for factory"]
    assert board.status_for(found, "queue") is None


# ---- Settings that name people or branches ------------------------------------------


def test_a_branch_pattern_must_accept_a_branch_with_a_jira_key(world):
    world.configure(JIRA + COLUMNS)
    path = world.repo / "factory.toml"
    path.write_text(path.read_text().replace(
        "[project]\n", '[project]\nbranch_pattern = "factory/[a-z0-9-]+"\n', 1))
    with pytest.raises(ConfigError) as caught:
        config.load(world.repo)
    assert "'factory/PROJ-123-000-add-dark-mode'" in "\n".join(caught.value.problems)
    path.write_text(path.read_text().replace('"factory/[a-z0-9-]+"', '"factory/[A-Za-z0-9-]+"'))
    assert config.load(world.repo)["project"]["branch_pattern"] == "factory/[A-Za-z0-9-]+"


def test_a_branch_pattern_without_jira_is_checked_without_a_key(world):
    path = world.repo / "factory.toml"
    path.write_text(path.read_text().replace(
        "[project]\n", '[project]\nbranch_pattern = "factory/[a-z0-9-]+"\n', 1))
    assert config.load(world.repo)["tracker"]["kind"] == "github"


def test_a_human_step_can_name_jira_accounts(world):
    named = """
[steps.ready-to-merge]
who = ["5b10ac8d82e05b22cc7d4ef5", "712020:8a7d2c6e-1f6b-4c8e-9d1a-2b3c4d5e6f70"]
"""
    world.configure(JIRA + COLUMNS + named)
    assert len(config.load(world.repo).steps["ready-to-merge"]["who"]) == 2


def test_a_github_login_still_cannot_hold_a_colon(world):
    found = problems(world, '[steps.ready-to-merge]\nwho = ["712020:8a7d2c6e"]\n')
    assert "[steps.ready-to-merge].who" in found
