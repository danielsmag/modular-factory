"""The pipeline on a board: which status each step's card sits in.

`[tracker.jira.columns]` maps a step, an end state or `queue` to a status name. Everything here is
read from that table and the steps of factory.toml, so no step and no column is named in code.
"""

from collections.abc import Callable
from typing import Any

from . import graph

QUEUE = "queue"


def _names(value: object) -> list[str] | None:
    """A status name, or a list of them, as a list. None when the value is neither."""
    if isinstance(value, str):
        return [value] if value.strip() else None
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return list(value)
    return None


def queue_statuses(columns: dict[str, Any]) -> list[str]:
    """The statuses new jobs are picked from."""
    return _names(columns.get(QUEUE)) or []


def status_for(columns: dict[str, Any], step: str) -> str | None:
    """The status a job's card sits in while the job is at `step`. None leaves the card where it is."""
    if step == QUEUE:
        return None
    value: object = columns.get(step)
    return value if isinstance(value, str) and value.strip() else None


def problems(steps: dict[str, Any], columns: object, label: str, needs_queue: bool) -> list[str]:
    """Every way the columns table is broken, each naming the key to fix."""
    if not isinstance(columns, dict):
        return [f"[{label}] must be a table of step = \"status\""]
    out: list[str] = []
    known: list[str] = [*steps, *sorted(graph.END_STATES)]
    for key, value in columns.items():
        where: str = f"[{label}].{key}"
        if key == QUEUE:
            if _names(value) is None:
                out.append(f"{where} must be a status name, or a list of them")
        elif key not in known:
            out.append(f"{where} is not a step. Steps: {', '.join(steps)}; end states: "
                       f"{', '.join(sorted(graph.END_STATES))}; and {QUEUE}")
        elif not isinstance(value, str) or not value.strip():
            out.append(f"{where} must be a status name")
    if needs_queue and QUEUE not in columns:
        out.append(f"[{label}].{QUEUE} is missing: the status new jobs are picked from")
    return out


# ---- Answering by moving the card ------------------------------------------


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def drags(steps: dict[str, Any], status_of: Callable[[str], str | None], name: str) -> dict[str, list[str]]:
    """For a step a person answers: each status its card can be moved to, and the outcomes that would mean.

    An outcome's status is the status of the step its edge leads to, after passing the steps that are
    switched off. The step's own status answers nothing. A status with two outcomes is ambiguous.
    """
    own: str | None = status_of(name)
    found: dict[str, list[str]] = {}
    for outcome, edge in steps[name].get("on", {}).items():
        target, _ = graph.resolve(steps, edge["to"])
        status: str | None = status_of(target)
        if status is None or (own is not None and _same(status, own)):
            continue
        shared: str = next((known for known in found if _same(known, status)), status)
        found.setdefault(shared, []).append(outcome)
    return found


def drag_outcomes(steps: dict[str, Any], status_of: Callable[[str], str | None], name: str,
                  status: str) -> list[str]:
    """The outcomes that moving the card of a job at `name` to `status` could mean. One is an answer."""
    for known, outcomes in drags(steps, status_of, name).items():
        if _same(known, status):
            return outcomes
    return []
