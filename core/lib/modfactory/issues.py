"""`mfactory issue`: a person's own work on the project's trackers, in or out of the factory's queue.

These commands list, show, open, comment on, move and close issues for the person at the keyboard. They never
start a job: `issue create` leaves the issue out of the queue unless `--queue` says otherwise, and a card that
has a job is moved with `mfactory move`, by step, so the job and its card stay together.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .graph import END_STATES
from .planner.holds import job_of
from .service.tracker import Key
from .util import Refused

if TYPE_CHECKING:
    from .engine import Factory

CLI = "mfactory"


def _clean(text: object, limit: int | None = None) -> str:
    """Tracker text on one line, without control characters that could move the cursor or recolour the terminal."""
    one: str = " ".join("".join(c for c in str(text or "") if c.isprintable() or c.isspace()).split())
    return one if limit is None or len(one) <= limit else one[:limit - 1] + "…"


def _people(people: object) -> list[str]:
    names: list[str] = []
    for person in people if isinstance(people, list) else []:
        if isinstance(person, dict):
            name: str = _clean(person.get("name") or person.get("login"))
            if name:
                names.append(name)
    return names


def _job(factory: Factory, key: Key) -> dict[str, Any] | None:
    job: dict[str, Any] | None = job_of(factory, key)
    return {"id": job["id"], "step": job["step"]} if job else None


def _row(factory: Factory, issue: dict[str, Any]) -> dict[str, Any]:
    key: Key = issue["number"]
    tracker: Any = factory.tracker.of(key)
    return {
        "key": tracker.ref(key), "tracker": tracker.kind, "title": _clean(issue.get("title")),
        "status": _clean(issue.get("status")) or ("open" if issue.get("state", "OPEN") == "OPEN" else "closed"),
        "assignees": _people(issue.get("assignees")), "url": issue.get("url"),
        "updated": issue.get("updatedAt"), "job": _job(factory, key),
    }


def _line(row: dict[str, Any]) -> str:
    who: str = ", ".join(row["assignees"]) or "unassigned"
    job: str = f"  [job {row['job']['id']} at {row['job']['step']}]" if row["job"] else ""
    return f"{row['key']}  {row['status']}  {who}  {_clean(row['title'], 80)}  {row['url'] or ''}{job}"


def list_issues(factory: Factory, kind: str | None, mine: bool, as_json: bool) -> int:
    """`issue list`: the open issues of one tracker, or of every tracker the project has."""
    source: Any = factory.tracker.member(kind) if kind else factory.tracker
    rows: list[dict[str, Any]] = [_row(factory, issue) for issue in source.open_issues(mine)]
    problems: list[str] = list(factory.tracker.problems)
    if as_json:
        print(json.dumps({"issues": rows, "errors": problems}))
        return 0
    for row in rows:
        print(_line(row))
    if not rows:
        print("no open issues" + (" assigned to you" if mine else ""))
    for problem in problems:
        print(f"note: {problem}")
    return 0


def show_issue(factory: Factory, ref: str, as_json: bool) -> int:
    """`issue show`: one issue with its text, comments, its job, and where its card can move."""
    key: Key = factory.tracker.key(ref)
    tracker: Any = factory.tracker.of(key)
    issue: dict[str, Any] = tracker.issue(key)
    row: dict[str, Any] = _row(factory, issue)
    row["body"] = str(issue.get("body") or "")
    row["author"] = (_people([issue.get("author")]) or [""])[0]
    row["comments"] = [{"author": (_people([c.get("author")]) or [""])[0], "created": c.get("createdAt"),
                        "body": str(c.get("body") or ""), "factory": bool(c.get("factory"))}
                       for c in issue.get("comments") or [] if isinstance(c, dict)]
    row["state"] = issue.get("state")
    row["moves_to"] = tracker.transitions(key) if row["state"] == "OPEN" else None
    if as_json:
        print(json.dumps(row))
        return 0
    print(f"{row['key']}: {row['title']}")
    print(f"status: {row['status']}  author: {row['author'] or '?'}  "
          f"assigned: {', '.join(row['assignees']) or 'nobody'}")
    print(f"url: {row['url']}")
    if row["job"]:
        print(f"job: {row['job']['id']} at {row['job']['step']}")
    if row["moves_to"]:
        print(f"can move to: {', '.join(row['moves_to'])}")
    print("")
    print(row["body"].strip() or "(no description)")
    for comment in row["comments"]:
        by: str = "the factory" if comment["factory"] else comment["author"] or "?"
        print("")
        print(f"--- {by}, {comment['created'] or ''}")
        print(comment["body"].strip())
    return 0


def create_issue(factory: Factory, kind: str | None, title: str, body: str, queue: bool) -> int:
    """`issue create`: open an issue. Only `--queue` puts it where the factory picks it up."""
    if not title.strip():
        raise Refused("the issue needs a title")
    target: Any = factory.tracker.member(kind) if kind else factory.tracker.primary
    url: str = target.create_issue(title.strip(), body, queue=queue)
    print(url)
    if not queue:
        print("note: not in the factory's queue; `mfactory issue create --queue` or "
              f"{target.enqueue_hint()} to have it built")
    return 0


def comment_issue(factory: Factory, ref: str, body: str) -> int:
    """`issue comment`: the person's own words on the issue, as they wrote them."""
    if not body.strip():
        raise Refused("the comment's text goes on stdin")
    key: Key = factory.tracker.key(ref)
    factory.tracker.of(key).comment(key, body, factory=False)
    print(f"commented on {factory.tracker.ref(key)}")
    return 0


def move_issue(factory: Factory, ref: str, status: str) -> int:
    """`issue move`: move a card that has no running job to another status of its board."""
    key: Key = factory.tracker.key(ref)
    job: dict[str, Any] | None = job_of(factory, key)
    shown: str = factory.tracker.ref(key)
    if job and job["step"] not in END_STATES:
        raise Refused(f"{shown} has job {job['id']} at {job['step']}, and its card follows the job: "
                      f"`{CLI} move {job['id']} <step>` moves the job, and its card with it")
    if not status.strip():
        raise Refused("name the status to move it to")
    moved: bool = factory.tracker.of(key).set_status(key, status.strip())
    print(f"moved {shown} to {status.strip()}" if moved else f"{shown} is already in {status.strip()}")
    return 0


def close_issue(factory: Factory, ref: str, not_planned: bool) -> int:
    """`issue close`: close an issue. One with a job is closed through it, so the job hears of it."""
    key: Key = factory.tracker.key(ref)
    job: dict[str, Any] | None = job_of(factory, key)
    if job:
        factory.close_issue(job["id"], not_planned=not_planned)
        return 0
    reason: str = "not planned" if not_planned else "completed"
    shown: str = factory.tracker.ref(key)
    if factory.tracker.of(key).close_issue(key, reason):
        print(f"closed {shown} ({reason})")
    else:
        print(f"{shown} was already closed")
    return 0
