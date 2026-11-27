"""Trackers: where jobs come from and where people talk to the factory.

`[tracker].kind` in factory.toml picks one, or a list of them. A new tracker is one
class here that follows `base.Tracker`, and one entry in TRACKERS. Two trackers are
used together through `routing.Trackers`.
"""

from pathlib import Path

from ... import config
from ...util import CORE, ConfigError, FactoryError
from ..gh import GitHub
from .base import Key, Tracker, Who
from .github import GitHubTracker
from .jira import JiraTracker
from .jira_acli import AcliJiraTracker, from_config
from .routing import Trackers

# Each builds its tracker from factory.toml. Jira picks how it reaches Jira: REST, or the Atlassian CLI.
TRACKERS = {"github": GitHubTracker.from_config, "jira": from_config}

__all__ = ["TRACKERS", "AcliJiraTracker", "GitHubTracker", "JiraTracker", "Key", "Tracker", "Trackers", "Who",
           "for_repo", "github_reader", "make"]


def make(cfg, gh: GitHub | None = None) -> Tracker | Trackers:
    """The tracker factory.toml asks for: the tracker itself, or both trackers through one router."""
    kinds: list[str] = config.tracker_kinds(cfg["tracker"]["kind"])
    if not kinds:
        raise ConfigError(f"[tracker].kind {config.KIND_RULE}")
    hub: GitHub = gh or GitHub(cfg.repo)
    built: list[Tracker] = [TRACKERS[kind](cfg, hub) for kind in kinds]
    return built[0] if len(built) == 1 else Trackers(built)


def for_repo(repo: Path) -> Tracker | Trackers:
    """The tracker of a checkout, for readers outside a job such as the dashboard.

    A factory.toml this version cannot read falls back to GitHub issues with the default labels.
    """
    try:
        return make(config.load(repo))
    except FactoryError:
        return github_reader(repo)


def github_reader(repo: Path) -> GitHubTracker:
    """GitHub issues with the default labels: for reading an issue of a checkout whatever its tracker."""
    defaults = config.read_toml(CORE / "defaults.toml")
    return GitHubTracker(GitHub(repo), defaults["labels"], defaults["project"]["issue_link"])
