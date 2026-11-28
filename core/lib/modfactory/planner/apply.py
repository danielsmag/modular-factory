"""Applying one approved planner action: the only code that lets a suggestion change a tracker or a job.

The scan agent's draft was checked when it was ingested, but `apply_action` checks the action again: it is
handed JSON on a command line, and it never trusts that the caller did.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..graph import END_STATES
from ..service.tracker import Key
from ..service.tracker.base import RELATION_TEXT
from ..util import Refused
from .context import add_context
from .holds import hold, job_of
from .ingest import ACTIONS, MAX_STEP, MAX_TEXT, OTHER_FIELDS, PAIRED, REF_FIELDS, RELATIONS

if TYPE_CHECKING:
    from ..engine import Factory

__all__ = ["RELATION_TEXT", "apply_action", "check", "describe", "first_target"]

# The relation as read from the other issue. A duplicate says nothing about the issue that was kept.
VERBS: dict[str, str] = {"duplicate": "is a duplicate of", "blocks": "blocks", "relates": "relates to"}
REVERSE: dict[str, str] = {"blocks": "blocked-by", "relates": "relates"}


def check(action: object, known: set[str]) -> dict[str, Any]:
    """The action, rebuilt from the fields its type allows, or Refused. Its refs must be in `known`."""
    if not isinstance(action, dict):
        raise Refused("the action is not an object")
    kind: object = action.get("type")
    if not isinstance(kind, str) or kind not in ACTIONS:
        raise Refused(f"action type {kind!r} is not one of {', '.join(ACTIONS)}")
    refs: tuple[str, ...] = REF_FIELDS[kind]
    fields: tuple[str, ...] = refs + OTHER_FIELDS.get(kind, ())
    extra: list[str] = sorted(set(action) - set(fields) - {"type"})
    if extra:
        raise Refused(f"action {kind}: unexpected field {extra[0]}")
    for name in fields:
        if name not in action:
            raise Refused(f"action {kind}: {name} is missing")
    for name in refs:
        value: object = action[name]
        if not isinstance(value, str) or value not in known:
            raise Refused(f"action {kind}: {name} {value!r} is not one of the finding's tickets")
    pair: tuple[str, str] | None = PAIRED.get(kind)
    if pair and action[pair[0]] == action[pair[1]]:
        raise Refused(f"action {kind}: {pair[0]} and {pair[1]} are the same ticket")
    if kind == "link" and action["relation"] not in RELATIONS:
        raise Refused(f"action link: relation {action['relation']!r} is not one of {', '.join(RELATIONS)}")
    if kind == "move":
        step: object = action["step"]
        if not isinstance(step, str) or not step or len(step) > MAX_STEP:
            raise Refused(f"action move: step must be a name of 1 to {MAX_STEP} characters")
        if step in END_STATES:
            raise Refused(f"action move: {step} is not a step to move a job to")
    if kind in ("context", "comment"):
        text: object = action["text"]
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise Refused(f"action {kind}: text must be a string of 1 to {MAX_TEXT} characters")
    return {"type": kind, **{name: action[name] for name in fields}}


def first_target(action: dict[str, Any]) -> str:
    """The ref of the ticket the action is carried out on: the project that owns it runs the action."""
    return str(action[REF_FIELDS[str(action["type"])][0]])


def _short(text: str, limit: int = 80) -> str:
    one: str = " ".join(text.split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def describe(action: dict[str, Any]) -> str:
    """One line saying what the action will do, for the person who approves it."""
    kind: str = str(action["type"])
    if kind == "link":
        return f"link {action['from']} {VERBS[action['relation']]} {action['to']}"
    if kind == "hold":
        return f"hold {action['job']} until {action['until']} is done"
    if kind == "close-duplicate":
        return f"close {action['close']} as not planned, as a duplicate of {action['keep']}"
    if kind == "context":
        return f"add context to {action['job']}: {_short(str(action['text']))}"
    if kind == "move":
        return f"move {action['job']} to step {action['step']}"
    return f"comment on {action['on']}: {_short(str(action['text']))}"


class _Refs:
    """Which refs belong to this project and which tickets they are, from what the caller passed."""

    def __init__(self, factory: Factory, refs: dict[str, tuple[Path, Key]], urls: dict[str, str] | None) -> None:
        self.factory: Factory = factory
        self.refs: dict[str, tuple[Path, Key]] = refs
        self.urls: dict[str, str] = urls or {}

    def local(self, ref: str) -> bool:
        return Path(self.refs[ref][0]).resolve() == Path(self.factory.cfg.repo).resolve()

    def key(self, ref: str) -> Key:
        """The key of a ticket of this project. Refused for a ticket that is in another project."""
        if not self.local(ref):
            raise Refused(f"{ref} is in another project, and this one cannot change it")
        return self.factory.tracker.key(str(self.refs[ref][1]))

    def text(self, ref: str) -> str:
        """What names the ticket to a tracker: its key here, else its URL, else the ref itself."""
        if self.local(ref):
            return str(self.factory.tracker.ref(self.key(ref)))
        return self.urls.get(ref) or ref


def _operator(factory: Factory, key: Key) -> str:
    return str(factory.tracker.of(key).operator())


def _link(refs: _Refs, source: str, target: str, relation: str, both: bool = True) -> str:
    factory: Factory = refs.factory
    key: Key = refs.key(source)
    tracker: Any = factory.tracker.of(key)
    tracker.link(key, refs.text(target), relation)
    said: str = f"linked {refs.text(source)}: {RELATION_TEXT[relation].lower()} {refs.text(target)}"
    if both and relation in REVERSE and refs.local(target):
        other: Key = refs.key(target)
        if not (tracker.links_both_ways and factory.tracker.of(other) is tracker):
            factory.tracker.of(other).link(other, refs.text(source), REVERSE[relation])
            said += f"; and {refs.text(target)}: {RELATION_TEXT[REVERSE[relation]].lower()} {refs.text(source)}"
    return said


def _close_duplicate(refs: _Refs, close: str, keep: str) -> str:
    factory: Factory = refs.factory
    said: str = _link(refs, close, keep, "duplicate", both=False)
    key: Key = refs.key(close)
    job: dict[str, Any] | None = job_of(factory, key)
    if job is None:
        if factory.tracker.close_issue(key, "not planned"):
            factory.tracker.comment(key, f"### Factory: issue closed by {_operator(factory, key)} (not planned)")
    else:
        factory.close_issue(job["id"], not_planned=True)
        if factory.store.read(job["id"])["step"] not in END_STATES:
            factory.close_job(job["id"])
    return f"{said}; closed {refs.text(close)} as not planned"


def apply_action(factory: Factory, action: object, workspace_refs: dict[str, tuple[Path, Key]],
                 urls: dict[str, str] | None = None) -> str:
    """Check `action` again, carry it out in this project, and return one line saying what was done.

    `workspace_refs` maps each ref the action may name to its project's path and its key there; a ref in another
    project reaches a tracker as its URL from `urls`, or as the ref text. Raises Refused for anything not allowed.
    """
    checked: dict[str, Any] = check(action, set(workspace_refs))
    refs: _Refs = _Refs(factory, workspace_refs, urls)
    kind: str = checked["type"]
    if kind == "link":
        return _link(refs, checked["from"], checked["to"], checked["relation"])
    if kind == "close-duplicate":
        return _close_duplicate(refs, checked["close"], checked["keep"])
    if kind == "comment":
        key: Key = refs.key(checked["on"])
        factory.tracker.comment(key, checked["text"], factory=True)
        return f"commented on {refs.text(checked['on'])}"
    key = refs.key(checked["job"])
    source: str = f"the planner, approved by {_operator(factory, key)}"
    if kind == "hold":
        until: str = checked["until"]
        project: Path = factory.cfg.repo if refs.local(until) else Path(workspace_refs[until][0])
        until_key: Key = refs.key(until) if refs.local(until) else workspace_refs[until][1]
        return hold(factory, key, until, project, until_key, source)
    if kind == "context":
        name: str = add_context(factory, key, checked["text"], source)
        return f"added {name} to {refs.text(checked['job'])}"
    job: dict[str, Any] | None = job_of(factory, key)
    if job is None:
        raise Refused(f"{refs.text(checked['job'])} has no job to move")
    factory.move(job["id"], checked["step"], f"suggested by {source}")
    return f"moved {job['id']} to {checked['step']}"
