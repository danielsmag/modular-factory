"""Trackers: where jobs come from and where people talk to the factory.

`[tracker].kind` in factory.toml picks one. A new tracker is one class here that
follows `base.Tracker`, and one entry in TRACKERS.
"""

from pathlib import Path

from ... import config
from ...util import CORE, FactoryError
from ..gh import GitHub
from .base import Key, Tracker, Who
from .github import GitHubTracker
from .jira import JiraTracker
from .jira_acli import AcliJiraTracker, from_config

# Each builds its tracker from factory.toml. Jira picks how it reaches Jira: REST, or the Atlassian CLI.
TRACKERS = {"github": GitHubTracker.from_config, "jira": from_config}

__all__ = ["TRACKERS", "AcliJiraTracker", "GitHubTracker", "JiraTracker", "Key", "Tracker", "Who", "for_repo",
           "make"]


def make(cfg, gh: GitHub | None = None) -> Tracker:
    """The tracker factory.toml asks for."""
    return TRACKERS[cfg["tracker"]["kind"]](cfg, gh or GitHub(cfg.repo))


def for_repo(repo: Path) -> Tracker:
    """The tracker of a checkout, for readers outside a job such as the dashboard.

    A factory.toml this version cannot read falls back to GitHub issues with the default labels.
    """
    try:
        return make(config.load(repo))
    except FactoryError:
        defaults = config.read_toml(CORE / "defaults.toml")
        return GitHubTracker(GitHub(repo), defaults["labels"], defaults["project"]["issue_link"])
