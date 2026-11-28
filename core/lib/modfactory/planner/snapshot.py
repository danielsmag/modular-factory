"""The tickets of one project: its queue, its jobs, and what each job's branch changes."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from ..engine import Factory
from ..graph import END_STATES
from ..service.tracker.base import key_kind
from ..util import FactoryError
from . import priority

EXCERPT = 1500


def branch_files(repo: Path, base: str, branch: str) -> list[str]:
    """The files `branch` changes against `base`. [] on any git error."""
    for start in (base, f"origin/{base}"):
        try:
            res: subprocess.CompletedProcess[str] = subprocess.run(
                ["git", "diff", "--name-only", f"{start}...{branch}"], cwd=repo, capture_output=True, text=True,
                timeout=60)
        except (OSError, subprocess.SubprocessError):
            return []
        if res.returncode == 0:
            return sorted(line for line in res.stdout.splitlines() if line.strip())
    return []


def _holds(factory: Factory) -> dict[str, dict[str, Any]]:
    """The holds file, read tolerantly: missing or corrupt means no holds."""
    try:
        found: object = json.loads((factory.cfg.repo / ".factory" / "jobs" / "holds.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(found, dict):
        return {}
    return {str(key): value for key, value in found.items() if isinstance(value, dict)}


def _hash(title: str, excerpt: str, labels: list[str], level: str) -> str:
    return hashlib.sha256(f"{title}{excerpt}{labels}{level}".encode()).hexdigest()[:12]


def _label_names(raw: dict[str, Any]) -> list[str]:
    return [str(label.get("name") if isinstance(label, dict) else label) for label in raw.get("labels") or []]


def project_tickets(factory: Factory, label: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Every queued issue and every job of this project as tickets, and what could not be read."""
    cfg: Any = factory.cfg
    errors: list[str] = []
    try:
        _, issues = factory.queue_and_issues()
    except FactoryError as error:
        errors.append(f"{label}: could not read the queue: {error}")
        issues = []
    settings: dict[str, Any] = cfg["planner"]
    project: dict[str, Any] = (settings.get("priority") or {}).get("github_project") or {}
    values: dict[int, str] = {}
    if project.get("owner") and project.get("number"):
        values = priority.github_project_levels(factory.gh, str(project["owner"]), int(project["number"]),
                                                str(project.get("field") or "Priority"))
    queued: dict[Any, dict[str, Any]] = {i["number"]: i for i in issues}
    jobs: dict[Any, dict[str, Any]] = {j["issue"]: j for j in factory.store.all()}
    holds: dict[str, dict[str, Any]] = _holds(factory)
    base: str = cfg["project"]["base_branch"]
    out: list[dict[str, Any]] = []
    for key in [*queued, *(k for k in jobs if k not in queued)]:
        issue: dict[str, Any] = queued.get(key) or {}
        job: dict[str, Any] | None = jobs.get(key)
        kind: str = (job or {}).get("tracker") or key_kind(key) or "github"
        title: str = str(issue.get("title") or (job or {}).get("title") or "")
        excerpt: str = ""
        files: list[str] = []
        if job:
            try:
                excerpt = (factory.dir(job) / "issue.md").read_text()[:EXCERPT]
            except OSError:
                excerpt = ""
            if job["step"] not in END_STATES and job.get("branch"):
                files = branch_files(cfg.repo, base, job["branch"])
        else:
            try:
                excerpt = str(factory.tracker.issue(key).get("body") or "")[:EXCERPT]
            except FactoryError as error:
                errors.append(f"{label}: could not read {factory.tracker.ref(key)}: {error}")
        try:
            ref: str = factory.tracker.ref(key)
        except FactoryError:
            ref = str(key)
        labels: list[str] = _label_names(issue)
        level: str = priority.level_for(kind, issue, settings, values)
        stage: str = job["step"] if job else "queued"
        until: str | None = None
        hold: dict[str, Any] | None = holds.get(str(key))
        if hold or (job and job.get("held_by")):
            stage = "held" if stage not in END_STATES else stage
            until = str(hold["until"]) if hold and hold.get("until") is not None else None
        out.append({
            "ref": f"{label}:{ref}", "project": label, "key": key, "tracker": kind, "title": title,
            "excerpt": excerpt, "labels": labels, "priority": level,
            "url": issue.get("url") or (job or {}).get("issue_url"), "stage": stage,
            "job": job["id"] if job else None, "branch": job.get("branch") if job else None, "files": files,
            "held_until": until, "hash": _hash(title, excerpt, labels, level)})
    return out, errors
