"""The pipeline on a board: which status each step's card sits in.

`[tracker.jira.columns]` maps a step, an end state or `queue` to a status name, and may list `watch`
statuses, whose cards are shown on the dashboard and never built. Everything here is
read from that table and the steps of factory.toml, so no step and no column is named in code.
"""

from collections.abc import Callable
from typing import Any

from . import graph

QUEUE = "queue"
WATCH = "watch"


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


def watch_statuses(columns: dict[str, Any]) -> list[str]:
    """The statuses whose cards the dashboard shows and the factory never builds."""
    return _names(columns.get(WATCH)) or []


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
        if key in (QUEUE, WATCH):
            if _names(value) is None:
                out.append(f"{where} must be a status name, or a list of them")
        elif key not in known:
            out.append(f"{where} is not a step. Steps: {', '.join(steps)}; end states: "
                       f"{', '.join(sorted(graph.END_STATES))}; {QUEUE}; and {WATCH}")
        elif not isinstance(value, str) or not value.strip():
            out.append(f"{where} must be a status name")
    out += _watch_overlaps(columns, label)
    if needs_queue and QUEUE not in columns:
        out.append(f"[{label}].{QUEUE} is missing: the status new jobs are picked from")
    return out


def _watch_overlaps(columns: dict[str, Any], label: str) -> list[str]:
    """A watched status that new jobs are picked from, or that a job's card moves to, would be built or moved."""
    out: list[str] = []
    for status in watch_statuses(columns):
        for key, value in columns.items():
            if key == WATCH:
                continue
            names: list[str] = (_names(value) or []) if key == QUEUE else [value] if isinstance(value, str) else []
            if any(_same(status, name) for name in names):
                out.append(f"[{label}].{WATCH}: `{status}` is also [{label}].{key}, so its cards would not "
                           "only be watched. Pick one")
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


# ---- What the board's workflow must allow ----------------------------------


def transitions(steps: dict[str, Any], status_of: Callable[[str], str | None], queue: list[str], start: str,
                escalate: str) -> list[tuple[str, str, str]]:
    """Every move of a card the pipeline can make, as (from status, to status, the edge that needs it).

    A step that is switched off is passed through, as a job passes through it. A step with no status
    keeps the card where it was, so the move goes on to the steps after it.
    """
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(a: str, b: str, why: str) -> None:
        pair: tuple[str, str] = (a.casefold(), b.casefold())
        if pair[0] != pair[1] and pair not in seen:
            seen.add(pair)
            out.append((a, b, why))

    def landings(target: str, trail: tuple[str, ...] = ()) -> list[str]:
        """The statuses a card can next be put in when its job goes to `target`."""
        target, _ = graph.resolve(steps, target)
        status: str | None = status_of(target)
        if status is not None:
            return [status]
        if target in graph.END_STATES or target in trail:
            return []
        found: list[str] = []
        for edge in steps[target].get("on", {}).values():
            found += landings(edge["to"], trail + (target,))
            if "max" in edge:
                found += landings(escalate, trail + (target,))
        return found

    for status in queue:
        for landing in landings(start):
            add(status, landing, "a new job")
    for name, step in steps.items():
        own: str | None = status_of(name)
        if own is None or not graph.enabled(step):
            continue
        for outcome, edge in step.get("on", {}).items():
            for landing in landings(edge["to"]):
                add(own, landing, f"{name} {outcome}")
            if "max" in edge:
                for landing in landings(escalate):
                    add(own, landing, f"{name} {outcome} reaches its max")
    return out


# ---- The board that works well for a pipeline ------------------------------

BACKLOG = "Backlog"
READY = "Ready for factory"
# Column names for the built-in steps. Any other step's column is named after the step.
NAMES = {"intake": "Intake", "ask-author": "Needs info", "spec": "Spec", "spec-approval": "Spec approval",
         "build": "Build", "review": "Review", "ready-to-merge": "Ready to merge", "merge": "Merging",
         "ready-to-verify": "Ready to verify", "verify": "Verify", "escalated": "Escalated"}
END_NAMES = {"done": "Done", "rejected": "Rejected", "failed": "Failed"}
# Steps that start a column of their own. Any other step joins the column of the step before it.
LEADING_ROLES = {"intake", "spec", "build"}
LEADING_TYPES = {"human", "review", "merge", "verify"}


def suggest(steps: dict[str, Any], start: str, escalate: str) -> dict[str, Any]:
    """The board that works well for this pipeline, left to right, and the `[tracker.jira.columns]` for it.

    A step a person answers gets a column of its own, and so does each step its outcomes lead to, so
    moving the card says one outcome. The factory's other steps share the column of the step before
    them. Steps that are switched off get no column.
    """
    live: dict[str, Any] = {n: s for n, s in steps.items() if graph.enabled(s)}
    declared: dict[str, int] = {n: i for i, n in enumerate(steps)}
    hand_merge: str | None = None
    for step in steps.values():
        if step.get("type") == "merge" and not graph.enabled(step) and "MERGED" in step.get("on", {}):
            hand_merge = graph.resolve(steps, step["on"]["MERGED"]["to"])[0]
            break

    def targets(name: str) -> list[str]:
        step: dict[str, Any] = live[name]
        found: list[str] = [graph.resolve(steps, edge["to"])[0] for edge in step.get("on", {}).values()]
        if hand_merge and step["type"] == "human" and step.get("pr_ready") and name != escalate:
            found.append(hand_merge)  # a person merges the pull request while the job waits here
        return found

    # Steps in the order a job meets them, the escalation step last.
    distance: dict[str, int] = {}
    for seed in (start, escalate):
        if seed not in live or seed in distance:
            continue
        distance[seed] = max(distance.values(), default=-1) + 1
        todo: list[str] = [seed]
        while todo:
            name = todo.pop(0)
            for target in targets(name):
                if target in live and target not in distance:
                    distance[target] = distance[name] + 1
                    todo.append(target)
    ordered: list[str] = sorted(distance, key=lambda n: (n == escalate, distance[n], declared[n]))

    before: dict[str, list[str]] = {n: [] for n in ordered}
    answers: set[str] = set()
    for name in ordered:
        for target in targets(name):
            if target in before and name not in before[target]:
                before[target].append(name)
            if live[name]["type"] == "human":
                answers.add(target)

    def leads(name: str) -> bool:
        step: dict[str, Any] = live[name]
        return (name == start or name in answers or step["type"] in LEADING_TYPES
                or (step["type"] == "agent" and graph.role(name, step) in LEADING_ROLES))

    taken: set[str] = {BACKLOG, READY, *END_NAMES.values()}
    columns: list[dict[str, Any]] = []
    column_of: dict[str, dict[str, Any]] = {}
    for name in ordered:
        host: dict[str, Any] | None = None
        if not leads(name):
            for prior in before[name]:
                if prior in column_of and live[prior]["type"] != "human":
                    host = column_of[prior]
                    break
        if host is None:
            title: str = NAMES.get(name) or name.replace("-", " ").replace("_", " ").capitalize()
            if title in taken:
                title = f"{title} ({name})"
            taken.add(title)
            host = {"name": title, "steps": [], "who": "you" if live[name]["type"] == "human" else "factory"}
            columns.append(host)
        host["steps"].append(name)
        column_of[name] = host

    ends: list[str] = [state for state in ("done", "rejected", "failed")
                       if state == "done" or any(state in targets(n) for n in ordered)]
    mapping: dict[str, str] = {QUEUE: READY}
    for host in columns:
        mapping.update({name: host["name"] for name in host["steps"]})
    mapping.update({state: END_NAMES[state] for state in ends})

    notes: list[str] = []
    for name in ordered:
        if live[name]["type"] != "human":
            continue
        for status, outcomes in drags(steps, mapping.get, name).items():
            if len(outcomes) > 1:
                notes.append(f"[steps.{name}]: {' and '.join(outcomes)} both lead to `{status}`, so moving the card "
                             "there answers nothing: answer them by comment")

    table: list[str] = ["[tracker.jira.columns]"] + [f'{key} = "{value}"' for key, value in mapping.items()]
    return {
        "columns": [{"name": BACKLOG, "steps": [], "who": "you"}, {"name": READY, "steps": [QUEUE], "who": "factory"},
                    *columns, *({"name": END_NAMES[s], "steps": [s], "who": "-"} for s in ends)],
        "mapping": mapping, "toml": "\n".join(table) + "\n", "notes": notes,
        "done_category": [END_NAMES[s] for s in ends],
    }
