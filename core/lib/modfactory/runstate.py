"""What state each step of a job's pipeline is in, from the job's history: for the dashboard's job graph.

A step is one of:
- `success`  its last visit ended with an outcome that moves the job on
- `retry`    its last visit sent the job back (FAIL, CHANGES, ...) and the job is still going
- `failed`   the job ended rejected or failed, and this step's last visit took it there or sent it back
- `running`  the job is in it and an agent or a script holds a slot
- `waiting`  the job is in it and a person must answer
- `queued`   the job is in it, waiting for a free slot
- `skipped`  it is switched off and the job passed through it
- `disabled` it is switched off and the job never passed it
- `none`     the job has not reached it
End states (`done`, `failed`, `rejected`) are `success` or `failed` when the job ended there.
"""

from datetime import datetime
from typing import Any

from . import graph

# Outcomes that send a job back, or out, rather than on.
BACK_OUTCOMES = {"FAIL", "FAILED", "CHANGES", "BLOCKED", "REJECT", "ESCALATE", "NEEDS-INFO", "UNKNOWN",
                 "FEEDBACK"}


def _seconds(start: object, end: object) -> float | None:
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max((b - a).total_seconds(), 0.0)


def job_status(job: dict[str, Any], steps: dict[str, Any]) -> str:
    """One word for the job: running, waiting, held, queued, done, failed or rejected."""
    current: object = job.get("step")
    if current in graph.END_STATES:
        return str(current)
    if job.get("held_by"):
        return "held"
    step: object = steps.get(current) if isinstance(current, str) else None
    if isinstance(step, dict) and step.get("type") == "human":
        return "waiting"
    if job.get("running_since"):
        return "running"
    return "queued"


def step_states(job: dict[str, Any], steps: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each step's state, its visits and the time spent in it, keyed by step name (end states included)."""
    history: list[dict[str, Any]] = [h for h in job.get("history") or [] if isinstance(h, dict)]
    current: object = job.get("step")
    finished: bool = current in graph.END_STATES
    out: dict[str, dict[str, Any]] = {}
    passed: set[str] = {name for h in history for name in (h.get("skipped") or []) if isinstance(name, str)}
    for name, step in steps.items():
        visits: list[dict[str, Any]] = [
            {"entered_at": h.get("entered_at"), "at": h.get("at"), "outcome": h.get("outcome"),
             "to": h.get("to"), "reason": h.get("reason"),
             "seconds": _seconds(h.get("entered_at"), h.get("at"))}
            for h in history if h.get("step") == name]
        seconds: float = sum(v["seconds"] or 0.0 for v in visits)
        state: str
        if name == current:
            state = job_status(job, steps)
        elif visits:
            back: bool = visits[-1]["outcome"] in BACK_OUTCOMES
            if back and not finished:
                state = "retry"
            elif back and current in ("failed", "rejected"):
                state = "failed"
            else:
                state = "success"  # moved on, or sent back in a round the job has since finished
        elif name in passed:
            state = "skipped"
        elif isinstance(step, dict) and not graph.enabled(step):
            state = "disabled"
        else:
            state = "none"
        out[name] = {"state": state, "visits": visits, "seconds": seconds,
                     "last_outcome": visits[-1]["outcome"] if visits else None,
                     "since": job.get("entered_at") if name == current else None}
    for end in sorted(graph.END_STATES):
        reached: bool = current == end
        out[end] = {"state": ("success" if end == "done" else "failed") if reached else "none",
                    "visits": [], "seconds": 0.0, "last_outcome": None,
                    "since": job.get("finished_at") if reached else None}
    return out


def edges_taken(job: dict[str, Any]) -> dict[str, int]:
    """How often the job left each step by each outcome: `"gate.FAIL": 2`."""
    taken: dict[str, int] = {}
    for h in job.get("history") or []:
        if isinstance(h, dict) and h.get("step") and h.get("outcome"):
            key: str = f"{h['step']}.{h['outcome']}"
            taken[key] = taken.get(key, 0) + 1
    return taken
