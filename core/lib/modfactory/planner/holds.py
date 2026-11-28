"""Holds: a job, or a queued issue with no job yet, waits until another job is done and takes no slot."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import graph
from ..service.tracker import Key
from ..util import Refused, now, write_atomic

if TYPE_CHECKING:
    from ..engine import Factory


def _file(factory: Factory) -> Path:
    return Path(factory.store.dir) / "holds.json"


def fresh_holds(factory: Factory) -> dict[str, dict[str, Any]]:
    """The holds of issues with no job yet, keyed by `str(key)`. A missing or corrupt file means none."""
    try:
        found: object = json.loads(_file(factory).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(found, dict):
        return {}
    return {str(key): value for key, value in found.items() if isinstance(value, dict)}


def _write(factory: Factory, found: dict[str, dict[str, Any]]) -> None:
    path: Path = _file(factory)
    if found:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(found, indent=2) + "\n")
    else:
        path.unlink(missing_ok=True)


def _norm(key: object) -> str:
    return str(key).lstrip("#")


def job_of(factory: Factory, key: Key) -> dict[str, Any] | None:
    for job in factory.store.all():
        if _norm(job["issue"]).lower() == _norm(key).lower():
            return job
    return None


def take_fresh(factory: Factory, key: Key) -> dict[str, Any] | None:
    """Remove and return the holds.json entry of `key`, for the job that was just made for its issue."""
    found: dict[str, dict[str, Any]] = fresh_holds(factory)
    entry: dict[str, Any] | None = found.pop(_norm(key), None)
    if entry is not None:
        _write(factory, found)
    return entry


def _restamp(factory: Factory, job: dict[str, Any]) -> None:
    """A job leaving a hold enters its slot afresh, behind the jobs that took slots meanwhile."""
    if factory.store.holds_slot(job):
        job["running_since"], job["running_order"] = now(), time.time_ns()
    else:
        job["running_since"] = job["running_order"] = None


def hold(factory: Factory, key: Key, until_ref: str, until_project: Path, until_key: Key, reason: str) -> str:
    """Hold the job of `key` until `until_ref` is done, or, with no job yet, note the hold in holds.json."""
    entry: dict[str, Any] = {"until": until_ref, "project": str(until_project), "key": until_key,
                             "reason": reason, "since": now()}
    ref: str = factory.tracker.ref(key)
    job: dict[str, Any] | None = job_of(factory, key)
    if job is None:
        found: dict[str, dict[str, Any]] = fresh_holds(factory)
        found[_norm(key)] = entry
        _write(factory, found)
        return f"held {ref}: until {until_ref} is done"
    if job["step"] in graph.END_STATES:
        raise Refused(f"{job['id']} is already finished ({job['step']}); there is nothing to hold")
    opened: dict[str, Any] = factory.store.open(job["id"])
    opened["held_by"] = entry
    opened["running_since"] = opened["running_order"] = None
    factory.store.save(opened)
    return f"held {opened['id']} {ref}: until {until_ref} is done"


def release(factory: Factory, key: Key, wait: bool = True) -> bool:
    """Drop the hold of `key`, from its job or from holds.json. False when nothing was held.

    With `wait` False a job another process is working on is left held (and False is returned): the next call retries.
    """
    freed: bool = False
    job: dict[str, Any] | None = job_of(factory, key)
    if job is not None and job.get("held_by"):
        opened: dict[str, Any] | None = factory.store.open(job["id"]) if wait else factory.store.try_open(job["id"])
        if opened is None:
            return False
        opened.pop("held_by", None)
        _restamp(factory, opened)
        factory.store.save(opened)
        freed = True
    found: dict[str, dict[str, Any]] = fresh_holds(factory)
    if found.pop(_norm(key), None) is not None:
        _write(factory, found)
        freed = True
    return freed


def blocker_done(factory: Factory, entry: dict[str, Any], queued: set[str] | None = None) -> bool | None:
    """Whether the awaited job is done. None when it lives in another project, which this one cannot judge.

    True when its job is finished, or it has no job and its issue has left the tracker's queue. `queued` is the
    keys the queue holds, when the caller has read it already.
    """
    if entry.get("project") != str(factory.cfg.repo):
        return None
    key: Key = entry["key"]
    job: dict[str, Any] | None = job_of(factory, key)
    if job is not None:
        return job["step"] in graph.END_STATES
    if queued is None:
        queued = {_norm(i["number"]) for i in factory.tracker.queue()}
    try:
        kind: str = factory.tracker.of(key).kind
    except Refused:
        return False
    if kind in getattr(factory.tracker, "unread", set()):
        return False  # the tracker could not be read, so an absent issue proves nothing
    return _norm(key) not in queued


def lift(factory: Factory, queued: set[str]) -> list[str]:
    """Move the holds of issues that have a job now onto it, then release every same-project hold that is done."""
    by_key: dict[str, dict[str, Any]] = {_norm(j["issue"]): j for j in factory.store.all()}
    found: dict[str, dict[str, Any]] = fresh_holds(factory)
    adopted: bool = False
    for key in [k for k in found if k in by_key]:
        if by_key[key]["step"] not in graph.END_STATES:
            opened: dict[str, Any] | None = factory.store.try_open(by_key[key]["id"])
            if opened is None:
                continue  # another process is working on it; the next queue adopts the hold
            opened["held_by"] = found[key]
            opened["running_since"] = opened["running_order"] = None
            factory.store.save(opened)
            by_key[key]["held_by"] = found[key]
        del found[key]
        adopted = True
    if adopted:
        _write(factory, found)
    entries: list[tuple[str, dict[str, Any]]] = [
        (key, j["held_by"]) for key, j in by_key.items() if isinstance(j.get("held_by"), dict)]
    entries += list(found.items())
    freed: list[str] = []
    for key, entry in entries:
        if blocker_done(factory, entry, queued) and release(factory, key, wait=False):
            freed.append(key)
    return freed
