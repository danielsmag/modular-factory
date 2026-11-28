"""The reproducibility hash ignores secrets and absolute paths."""

from pathlib import Path

from modfactory.telemetry.fingerprint import Fingerprint, fingerprint_config


class _Config:
    def __init__(self, data: dict[str, object], repo: Path) -> None:
        self.data: dict[str, object] = data
        self.repo: Path = repo
        self.tools: list[str] = ["claude"]
        self.base: str = "main"
        self.steps: dict[str, object] = {}

    def pipeline_of(self, job: dict[str, object] | None) -> "_Config":
        return self


def test_hash_ignores_secrets_and_changes_when_execution_config_changes(tmp_path: Path) -> None:
    repo: Path = tmp_path / "repo"
    repo.mkdir()
    base: dict[str, object] = {
        "project": {"description": "clinics"},
        "tracker": {"jira": {"email": "a@b.c", "token_file": "/Users/me/.token", "site": "https://acme.atlassian.net"}},
        "run": {"concurrency": 3, "start": "intake", "escalate_to": "escalated"},
    }
    first: Fingerprint = fingerprint_config(_Config(base, repo))
    changed_secret: dict[str, object] = {
        "project": {"description": "clinics"},
        "tracker": {"jira": {"email": "other@b.c", "token_file": "/tmp/other", "site": "https://acme.atlassian.net"}},
        "run": {"concurrency": 3, "start": "intake", "escalate_to": "escalated"},
    }
    second: Fingerprint = fingerprint_config(_Config(changed_secret, repo))
    changed_run: dict[str, object] = {
        "project": {"description": "clinics"},
        "tracker": {"jira": {"email": "a@b.c", "token_file": "/Users/me/.token", "site": "https://acme.atlassian.net"}},
        "run": {"concurrency": 4, "start": "intake", "escalate_to": "escalated"},
    }
    third: Fingerprint = fingerprint_config(_Config(changed_run, repo))
    assert first.factory_config_hash == second.factory_config_hash
    assert first.factory_config_hash != third.factory_config_hash
    assert first.adapter == "claude"
