"""Two trackers as one: each issue goes to the tracker its key belongs to, and their queues take turns.

`make()` builds this only when `[tracker].kind` names two trackers. A single tracker is used as it is,
with the same set calls from `base.OneTracker`, so a call site is written once for one tracker or two.

A call that takes an issue's key goes to that issue's tracker. What differs per tracker and needs no key,
such as `pr_threads` or `operator()`, is not here: a call site asks `of(key)` for the job's own tracker.
"""

from itertools import zip_longest
from typing import Any

from ...util import FactoryError, Refused
from .base import LABELS, Key, Tracker, Who, key_kind, left_kind

# How each tracker's pull request rule starts, so the build agent can tell which one applies.
RULE_INTRO = {"github": "If the issue is a number such as 12: ", "jira": "If the issue is a key such as PROJ-123: "}


class Trackers:
    def __init__(self, members: list[Tracker]) -> None:
        self.members: list[Tracker] = members
        # What could not be read, and the kinds of the trackers it failed for. Filled as calls fail.
        self.problems: list[str] = []
        self.unread: set[str] = set()
        self._errors: dict[str, FactoryError] = {}

    # -- the set

    @property
    def primary(self) -> Tracker:
        """The first tracker in `[tracker].kind`: where the factory files new issues."""
        return self.members[0]

    def member(self, kind: str) -> Tracker:
        for tracker in self.members:
            if tracker.kind == kind:
                return tracker
        names: str = ", ".join(tracker.kind for tracker in self.members)
        raise Refused(f"this project takes no jobs from {LABELS.get(kind, kind)}: [tracker].kind is {names}")

    def board_member(self) -> Tracker | None:
        return next((tracker for tracker in self.members if tracker.has_board), None)

    def of(self, key: Key) -> Tracker:
        """The tracker that holds `key`."""
        owner: str | None = key_kind(key)
        if owner is None:
            raise Refused(self._not_a_key(key))
        for tracker in self.members:
            if tracker.kind == owner:
                return tracker
        raise Refused(left_kind(key, owner))

    def _not_a_key(self, ref: object) -> str:
        return (f"{str(ref).strip()!r} is not an issue: use a GitHub issue number, such as 12, "
                "or a Jira key, such as PROJ-123")

    # -- keys

    def key(self, ref: object) -> Key:
        return self.of(str(ref).strip()).key(ref)

    def ref(self, key: Key) -> str:
        return self.of(key).ref(key)

    # -- issues and the queue

    def issue(self, key: Key) -> dict[str, Any]:
        return self.of(key).issue(key)

    def issue_details(self, key: Key) -> object:
        return self.of(key).issue_details(key)

    def queue(self) -> list[dict[str, Any]]:
        """Every tracker's queue, taken in turn from the first: each keeps its own order, and none waits.

        A tracker that cannot be read is left out, named in `problems` and `unread`, so the other's jobs
        still start. When none can be read, the first failure is raised.
        """
        queues: list[list[dict[str, Any]]] = []
        for tracker in self.members:
            if tracker.kind in self.unread:
                continue  # it already failed in this run; asking again would only wait for it again
            try:
                queues.append(tracker.queue())
            except FactoryError as error:
                self._unreadable(tracker, error)
        if len(self.unread) == len(self.members):
            raise self._errors[self.members[0].kind]
        return [issue for turn in zip_longest(*queues) for issue in turn if issue is not None]

    def _unreadable(self, tracker: Tracker, error: FactoryError) -> None:
        """Remember that `tracker` could not be read, once, so the other tracker's jobs carry on."""
        if tracker.kind not in self.unread:
            self.unread.add(tracker.kind)
            self._errors[tracker.kind] = error
            lines: list[str] = str(error).strip().splitlines()
            self.problems.append(f"{tracker.label}: {lines[-1] if lines else 'could not be read'}")

    def failed(self, tracker: Tracker, what: object, error: FactoryError) -> str:
        """Note that one call for `what` (an issue, or `@me`) failed, while `tracker` itself answers.

        Only that job is held back: the tracker stays readable for every other job. Returns the reason.
        """
        lines: list[str] = str(error).strip().splitlines()
        why: str = lines[-1] if lines else "could not be read"
        problem: str = f"{tracker.label}: {what}: {why}"
        if problem not in self.problems:
            self.problems.append(problem)
        return why

    def reason(self, kind: str) -> str:
        """Why the tracker of `kind` could not be read, for a job that waits on it."""
        label: str = LABELS.get(kind, kind)
        return next((problem.removeprefix(f"{label}: ") for problem in self.problems
                     if problem.startswith(f"{label}: ")), "")

    def watched(self) -> list[dict[str, Any]]:
        """Every tracker's watched issues, for the dashboard. A tracker that cannot be read shows none."""
        found: list[dict[str, Any]] = []
        for tracker in self.members:
            if tracker.kind in self.unread:
                continue
            try:
                found += tracker.watched()
            except FactoryError as error:
                self._unreadable(tracker, error)
        return found

    def withdrawn(self, key: Key, queued: set[Key]) -> str | None:
        return self.of(key).withdrawn(key, queued)

    def queue_note(self) -> str:
        return "; ".join(f"{tracker.label}: {tracker.queue_note()}" for tracker in self.members)

    def empty_note(self) -> str:
        return ", ".join(tracker.empty_note() for tracker in self.members)

    def enqueue_hint(self) -> str:
        hints: list[str] = [tracker.enqueue_hint() for tracker in self.members]
        return hints[0] + "".join(f", or {hint[:1].lower()}{hint[1:]}" for hint in hints[1:])

    def queue_gate(self, issue: dict[str, Any]) -> str | None:
        return self.of(issue["number"]).queue_gate(issue)

    def dequeue(self, key: Key) -> None:
        self.of(key).dequeue(key)

    def create_issue(self, title: str, body: str, queue: bool = False) -> str:
        return self.primary.create_issue(title, body, queue=queue)

    def close_issue(self, key: Key, reason: str = "completed") -> bool:
        return self.of(key).close_issue(key, reason)

    # -- comments

    def comments(self, key: Key) -> list[dict[str, Any]]:
        return self.of(key).comments(key)

    def comment(self, key: Key, body: str, factory: bool = True) -> None:
        self.of(key).comment(key, body, factory=factory)

    def link(self, key: Key, other: str, relation: str) -> None:
        self.of(key).link(key, other, relation)

    # -- people

    def can_write(self, login: str, key: Key) -> bool:
        return self.of(key).can_write(login, key)

    # -- waiting for a person

    def waiting_on(self, key: Key, who: Who) -> str | None:
        return self.of(key).waiting_on(key, who)

    def waiting_off(self, key: Key, flag: str | None) -> None:
        self.of(key).waiting_off(key, flag)

    # -- the board

    def move(self, key: Key, step: str) -> None:
        self.of(key).move(key, step)

    def statuses(self, keys: list[Key]) -> dict[Key, str]:
        """Where each card is, asked of each tracker for its own keys. A tracker that fails leaves its keys out."""
        out: dict[Key, str] = {}
        for tracker in self.members:
            mine: list[Key] = [key for key in keys if key_kind(key) == tracker.kind]
            if mine and tracker.kind not in self.unread:
                try:
                    out.update(tracker.statuses(mine))
                except FactoryError as error:
                    self._unreadable(tracker, error)
        return out

    def last_move(self, key: Key, status: str) -> dict[str, Any] | None:
        return self.of(key).last_move(key, status)

    def board_report(self) -> dict[str, Any] | None:
        found: Tracker | None = self.board_member()
        return found.board_report() if found else None

    # -- the pull request's link to the issue

    def branch_tag(self, key: Key) -> str:
        return self.of(key).branch_tag(key)

    def pr_title_problem(self, title: str, key: Key) -> str | None:
        return self.of(key).pr_title_problem(title, key)

    def pr_link_rule(self) -> str:
        return "\n\n".join(RULE_INTRO.get(tracker.kind, "") + tracker.pr_link_rule() for tracker in self.members)

    def pr_link_problem(self, body: str, key: Key) -> str | None:
        return self.of(key).pr_link_problem(body, key)

    # -- install, doctor and remove

    def install(self, say) -> None:
        for tracker in self.members:
            tracker.install(say)

    def doctor(self, ok, warn, fail) -> None:
        for tracker in self.members:
            tracker.doctor(ok, warn, fail)

    def uninstall(self, say) -> None:
        for tracker in self.members:
            tracker.uninstall(say)
