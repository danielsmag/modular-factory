"""Run each project's own `mfactory queue`, then apply the fleet-wide cap."""

from __future__ import annotations

from collections.abc import Callable
from subprocess import CompletedProcess

from ..util import run
from .concurrency import QueueRow, cap_rows, default_concurrency, parse_concurrency
from .project import Project


def collect_queue(
    projects: list[Project],
    label_of: Callable[[Project], str],
    saved_limit: int | None,
    concurrency: int | None = None,
) -> tuple[list[QueueRow], list[str]]:
    """Rows of `project action issue job step note`, and one error string per project that failed.

    Each project has already applied its own `[run].concurrency`. This cap is
    across projects: `--concurrency`, then `fleet.toml`, then `[fleet].concurrency`
    in defaults.toml. It keeps that many `new` and `resume` rows, in project
    order, and marks the rest `hold`. `wait`, `skip` and `hold` do not take a slot.
    """
    limit: int | None = saved_limit if saved_limit is not None else default_concurrency()
    if concurrency is not None:
        limit = parse_concurrency(concurrency, "--concurrency")
    rows: list[QueueRow] = []
    errors: list[str] = []
    for project in projects:
        label: str = label_of(project)
        if not project.binary.is_file():
            errors.append(f"{label}: .factory/bin/mfactory is missing")
            continue
        try:
            result: CompletedProcess[str] = run([str(project.binary), "queue"], cwd=project.path, check=False)
        except FileNotFoundError:
            errors.append(f"{label}: .factory/bin/mfactory could not be run")
            continue
        if result.returncode != 0:
            detail: list[str] = (result.stderr or result.stdout).strip().splitlines()
            errors.append(f"{label}: {detail[-1] if detail else 'mfactory queue failed'}")
            continue
        for line in result.stdout.splitlines():
            cells: list[str] = line.split("  ", 4)
            if len(cells) != 5:
                continue
            action, issue, job, step, note = cells
            rows.append((label, action, issue, job, step, note))
    if limit is not None:
        rows = cap_rows(rows, limit)
    return rows, errors
