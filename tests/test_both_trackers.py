"""One project that takes jobs from GitHub issues and a Jira board at once."""

import pytest
from conftest import JIRA_COLUMNS
from modfactory import config
from modfactory.util import ConfigError

BOTH = """
[tracker]
kind = ["github", "jira"]

[tracker.jira]
site = "https://acme.atlassian.net"
board = 7
"""
ONLY_JIRA = BOTH.replace('kind = ["github", "jira"]', 'kind = "jira"')


def problems(world, text, project=""):
    configure(world, text, project)
    with pytest.raises(ConfigError) as caught:
        config.load(world.repo)
    return "\n".join(caught.value.problems)


def configure(world, text, project=""):
    """`text` after the base config, and `project` lines inside its existing [project] table."""
    world.configure(text)
    path = world.repo / "factory.toml"
    path.write_text(path.read_text().replace("[project]\n", "[project]\n" + project, 1))


# ---- [tracker].kind ---------------------------------------------------------------


def test_kind_takes_a_list_of_both_trackers(world):
    world.configure(BOTH + JIRA_COLUMNS)
    assert config.tracker_kinds(config.load(world.repo)["tracker"]["kind"]) == ["github", "jira"]


@pytest.mark.parametrize("kind", ["[]", '["jira", "jira"]', '["gitlab"]', "5"])
def test_kind_refuses_empty_duplicate_unknown_and_junk(world, kind):
    found = problems(world, BOTH.replace('["github", "jira"]', kind) + JIRA_COLUMNS)
    assert '[tracker].kind must be "github", "jira", or a list of them' in found


def test_tracker_kinds_reads_a_string_a_list_or_junk():
    assert config.tracker_kinds("jira") == ["jira"]
    assert config.tracker_kinds(["jira", "github"]) == ["jira", "github"]
    assert config.tracker_kinds(5) == []
    assert config.tracker_kinds(["github", 3]) == []


# ---- People, named per tracker ----------------------------------------------------


def test_two_trackers_need_people_tables(world):
    found = problems(world, BOTH + JIRA_COLUMNS + '\n[run]\nauthors = ["alice"]\n')
    assert "[run].authors" in found
    assert "authors = { github = [...], jira = [...] }" in found
    world.configure(BOTH + JIRA_COLUMNS + '\n[run]\nauthors = { github = ["alice"], jira = ["712020:abc"] }\n')
    assert config.load(world.repo)["run"]["authors"] == {"github": ["alice"], "jira": ["712020:abc"]}


def test_people_tables_check_each_entry_by_its_tracker(world):
    found = problems(world, BOTH + JIRA_COLUMNS + '\n[run]\nassignees = { github = ["bad login!"] }\n')
    assert "[run].assignees.github" in found
    assert "GitHub logins" in found


def test_a_people_table_names_only_known_trackers(world):
    found = problems(world, BOTH + JIRA_COLUMNS + '\n[run]\nauthors = { gitlab = ["alice"] }\n')
    assert "[run].authors.gitlab" in found


def test_who_table_must_name_every_tracker(world):
    step = '\n[steps.spec-approval]\nenabled = true\nwho = { github = ["alice"] }\n'
    found = problems(world, BOTH + JIRA_COLUMNS + step)
    assert "[steps.spec-approval].who" in found
    assert "jira" in found
    world.configure(BOTH + JIRA_COLUMNS
                    + '\n[steps.spec-approval]\nenabled = true\nwho = { github = ["alice"], jira = ["712020:abc"] }\n')
    assert config.load(world.repo).steps["spec-approval"]["who"] == {"github": ["alice"], "jira": ["712020:abc"]}


def test_a_who_word_stays_a_plain_string_with_two_trackers(world):
    world.configure(BOTH + JIRA_COLUMNS + '\n[steps.spec-approval]\nenabled = true\nwho = "operator"\n')
    assert config.load(world.repo).steps["spec-approval"]["who"] == "operator"


def test_one_tracker_keeps_plain_people_lists(world):
    world.configure('[run]\nauthors = ["alice"]\n')
    assert config.load(world.repo)["run"]["authors"] == ["alice"]
    assert "[run].authors" in problems(world, '[run]\nauthors = { github = ["alice"] }\n')


def test_people_for_and_who_for_resolve_one_tracker():
    assert config.people_for({"github": ["a"]}, "jira") == []
    assert config.people_for({"github": ["a"], "jira": ["b"]}, "jira") == ["b"]
    assert config.people_for(["a"], "github") == ["a"]
    assert config.people_for(None, "github") == []
    assert config.who_for({"who": {"github": ["a"], "jira": ["b"]}}, "jira") == ["b"]
    assert config.who_for({"who": ["a"]}, "jira") == ["a"]
    assert config.who_for({"who": "author"}, "jira") == "author"
    assert config.who_for({}, "github") == "maintainer"


# ---- Rules that differed per tracker ----------------------------------------------


def test_closes_is_allowed_with_both_trackers(world):
    closes = 'issue_link = "closes"\n'
    configure(world, BOTH + JIRA_COLUMNS, closes)
    assert config.load(world.repo)["project"]["issue_link"] == "closes"
    assert 'issue_link = "closes" needs GitHub issues' in problems(world, ONLY_JIRA + JIRA_COLUMNS, closes)


def test_a_branch_pattern_must_accept_branches_with_and_without_a_jira_key(world):
    # Accepts `factory/012-...` but not `factory/PROJ-123-012-...`.
    found = problems(world, BOTH + JIRA_COLUMNS, 'branch_pattern = "factory/[0-9][a-z0-9-]+"\n')
    assert "PROJ-123-" in found
    configure(world, BOTH + JIRA_COLUMNS, 'branch_pattern = "factory/[A-Za-z0-9-]+"\n')
    assert config.load(world.repo)["project"]["branch_pattern"] == "factory/[A-Za-z0-9-]+"


def test_jira_still_needs_a_site_and_a_board_with_both(world):
    found = problems(world, '[tracker]\nkind = ["github", "jira"]\n' + JIRA_COLUMNS)
    assert "[tracker.jira].site is missing" in found
    assert "[tracker.jira].board is missing" in found
