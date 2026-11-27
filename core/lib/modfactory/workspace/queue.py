"""Run each project's own `mfactory queue`, then apply the workspace-wide cap."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from subprocess import CompletedProcess
from typing import TypeAlias

from ..activity import redact
from ..util import Refused, run
from .concurrency import QueueRow, cap_rows, default_concurrency, parse_concurrency
from .project import Project

ProjectRow: TypeAlias = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class QueueMeta:
    """What `gh issue list` said about a row's issue. Empty for a project core older than `queue --json`."""

    title: str | None = None
    issue_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    author: str | None = None
    labels: tuple[str, ...] = ()


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _json_rows(text: str) -> list[tuple[ProjectRow, QueueMeta]]:
    """Rows of `mfactory queue --json`. Refused when the array or one of its rows is malformed."""
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise Refused("mfactory queue --json printed malformed JSON") from error
    if not isinstance(parsed, list):
        raise Refused("mfactory queue --json did not print a JSON array")
    out: list[tuple[ProjectRow, QueueMeta]] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise Refused("mfactory queue --json printed a row that is not an object")
        issue: object = item.get("issue")
        cells: list[object] = [item.get("action"), item.get("job"), item.get("step"), item.get("note")]
        if isinstance(issue, bool) or not isinstance(issue, int | str) or not all(isinstance(c, str) for c in cells):
            raise Refused("mfactory queue --json printed a row without action, issue, job, step and note")
        labels: object = item.get("labels")
        meta: QueueMeta = QueueMeta(
            title=_optional_text(item.get("title")),
            issue_url=_optional_text(item.get("issue_url")),
            created_at=_optional_text(item.get("created_at")),
            updated_at=_optional_text(item.get("updated_at")),
            author=_optional_text(item.get("author")),
            labels=tuple(name for name in labels if isinstance(name, str)) if isinstance(labels, list) else (),
        )
        action, job, step, note = (str(cell) for cell in cells)
        out.append(((action, str(issue), job, step, note), meta))
    return out


def _text_rows(text: str) -> list[tuple[ProjectRow, QueueMeta]]:
    out: list[tuple[ProjectRow, QueueMeta]] = []
    for line in text.splitlines():
        cells: list[str] = line.split("  ", 4)
        if len(cells) != 5:
            continue
        action, issue, job, step, note = cells
        out.append(((action, issue, job, step, note), QueueMeta()))
    return out


def _run_queue(project: Project) -> CompletedProcess[str]:
    """`queue --json`, or plain `queue` when the project's pinned core does not know the flag."""
    result: CompletedProcess[str] = run([str(project.binary), "queue", "--json"], cwd=project.path, check=False)
    if result.returncode != 0 and "unrecognized arguments" in (result.stderr or ""):
        result = run([str(project.binary), "queue"], cwd=project.path, check=False)
    return result


def collect_queue(
    projects: list[Project],
    label_of: Callable[[Project], str],
    saved_limit: int | None,
    concurrency: int | None = None,
) -> tuple[list[QueueRow], list[QueueMeta], list[str]]:
    """Rows of `project action issue job step note`, each row's issue metadata, and one error per failed project.

    Each project has already applied its own `[run].concurrency`. This cap is
    across projects: `--concurrency`, then `workspace.toml`, then `[workspace].concurrency`
    in defaults.toml. It keeps that many `new` and `resume` rows, in project
    order, and marks the rest `hold`. `wait`, `skip` and `hold` do not take a slot.
    """
    limit: int | None = saved_limit if saved_limit is not None else default_concurrency()
    if concurrency is not None:
        limit = parse_concurrency(concurrency, "--concurrency")
    rows: list[QueueRow] = []
    metas: list[QueueMeta] = []
    errors: list[str] = []
    for project in projects:
        label: str = label_of(project)
        if not project.binary.is_file():
            errors.append(f"{label}: .factory/bin/mfactory is missing")
            continue
        try:
            result: CompletedProcess[str] = _run_queue(project)
        except FileNotFoundError:
            errors.append(f"{label}: .factory/bin/mfactory could not be run")
            continue
        if result.returncode != 0:
            detail: list[str] = (result.stderr or result.stdout).strip().splitlines()
            errors.append(f"{label}: {detail[-1] if detail else 'mfactory queue failed'}")
            continue
        parsed: list[tuple[ProjectRow, QueueMeta]]
        try:
            parsed = _json_rows(result.stdout) if result.stdout.strip().startswith("[") else _text_rows(result.stdout)
        except Refused as error:
            errors.append(f"{label}: {error}")
            continue
        for (action, issue, job, step, note), meta in parsed:
            rows.append((label, action, issue, job, step, note))
            metas.append(meta)
        # A project with two trackers still answers when one of them could not be read, and says which.
        errors += [f"{label}: {redact(line.removeprefix('problem: ').strip())}"
                   for line in (result.stderr or "").splitlines() if line.startswith("problem: ")]
    if limit is not None:
        rows = cap_rows(rows, limit)
    return rows, metas, errors
