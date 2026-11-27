"""What the factory asks of a tracker: the place jobs come from and people reply in."""

import re
from typing import Any, Protocol

from ...util import Refused

# An issue's key, as the tracker names it: a number on GitHub.
Key = int | str
# Who a human step waits for: "author", "maintainer", "operator", or a list of logins.
Who = str | list[str]
# A Jira issue key, such as PROJ-123. GitHub numbers its issues.
JIRA_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*")
# Each tracker as people say it.
LABELS = {"github": "GitHub", "jira": "Jira"}


def key_kind(ref: object) -> str | None:
    """Which tracker a key belongs to, by its shape: `12` or `#12` is GitHub's, `PROJ-123` is Jira's."""
    text: str = str(ref).strip()
    if text.lstrip("#").isdigit():
        return "github"
    if JIRA_KEY.fullmatch(text):
        return "jira"
    return None


def left_kind(key: object, kind: str) -> str:
    """Why a key cannot be routed: its tracker is not one of the project's."""
    return (f"{key} is a {LABELS[kind]} issue, and [tracker].kind does not include {kind}, "
            "so the factory cannot reach it")


class Tracker(Protocol):
    """One tracker. The engine calls only these, so it works the same for any of them.

    An issue is a dict with `number` (its key), `title`, `body`, `state` (`OPEN` or not), `url`,
    `author` (`{"login": ...}`), `labels`, and `comments`. A comment has `id`, `author`, `body`,
    `createdAt`, and `factory`: True when the factory wrote it.
    """

    kind: str
    # The tracker's name, as people say it.
    label: str
    # True when a person may be asked, and may reply, on the job's pull request too.
    pr_threads: bool
    # The longest comment the tracker takes, in characters.
    comment_limit: int
    # True when the pull request's title must carry the issue's key.
    links_by_title: bool
    # True when a person can answer a human step by moving the issue's card on a board.
    drag_answers: bool
    # True when each step's card sits in a column of a board.
    has_board: bool

    # -- keys

    def key(self, ref: object) -> Key:
        """The key a person typed, as jobs store it. Refuses text that is not a key."""
        ...

    def ref(self, key: Key) -> str:
        """The key as people write it."""
        ...

    # -- issues and the queue

    def issue(self, key: Key) -> dict[str, Any]: ...

    def issue_details(self, key: Key) -> object:
        """The issue for the dashboard's drawer."""
        ...

    def queue(self) -> list[dict[str, Any]]:
        """The open issues waiting for a job, in the order to start them."""
        ...

    def watched(self) -> list[dict[str, Any]]:
        """Open issues the dashboard shows as coming up and the factory never builds. Often none."""
        ...

    def withdrawn(self, key: Key, queued: set[Key]) -> str | None:
        """Why a running job's issue no longer asks to be built, or None. `queued` is the keys in `queue`."""
        ...

    def queue_note(self) -> str:
        """How the queue is kept, as a clause."""
        ...

    def empty_note(self) -> str:
        """What an empty queue means, as the start of a sentence."""
        ...

    def enqueue_hint(self) -> str:
        """How a person puts an issue in the queue for the first time, as a short imperative."""
        ...

    def requeue_hint(self) -> str:
        """How a person puts an issue back in the queue."""
        ...

    def queue_gate(self, issue: dict[str, Any]) -> str | None:
        """Why this issue may not start a job, or None when it is in the queue."""
        ...

    def dequeue(self, key: Key) -> None:
        """Take a rejected issue out of the queue."""
        ...

    def create_issue(self, title: str, body: str, queue: bool = False) -> str:
        """Open an issue and return its URL. With `queue`, it waits for a job."""
        ...

    def close_issue(self, key: Key, reason: str = "completed") -> bool:
        """Close the issue. False when it was already closed."""
        ...

    # -- comments

    def comments(self, key: Key) -> list[dict[str, Any]]: ...

    def comment(self, key: Key, body: str, factory: bool = True) -> None:
        """Post a comment. `factory=False` posts a person's own words, which count as a reply."""
        ...

    # -- people

    def operator(self) -> str:
        """The account running the factory."""
        ...

    def mention(self, login: str, name: str | None = None) -> str:
        """An account as text for people to read. `name` is its shown name, when the caller has it."""
        ...

    def can_write(self, login: str, key: Key) -> bool:
        """True for the operator and for anyone who may answer a maintainer step of this issue."""
        ...

    # -- waiting for a person

    def waiting_on(self, key: Key, who: Who) -> str | None:
        """Show on the issue that a person is asked. Returns what `waiting_off` needs to undo it."""
        ...

    def waiting_off(self, key: Key, flag: str | None) -> None: ...

    # -- the board

    def move(self, key: Key, step: str) -> None:
        """Show on the board that the issue's job is at `step`. Refuses when the card will not move."""
        ...

    def status_for(self, step: str) -> str | None:
        """The board status of a job at `step`. None when the step has none, or the tracker has no board."""
        ...

    def statuses(self, keys: list[Key]) -> dict[Key, str]:
        """Where each card is now. A key that is left out is unknown."""
        ...

    def last_move(self, key: Key, status: str) -> dict[str, Any] | None:
        """The newest move of the card into `status`: `by` (a person), `from` and `to`. None when unknown.

        `by` is None when the tracker sees that the card moved and cannot tell who moved it; `why` then
        says so, for the comment that goes with moving the card back.
        """
        ...

    def board_report(self) -> dict[str, Any] | None:
        """The steps against the live board: `rows`, `unused`, `problems`, `unchecked`. None with no board."""
        ...

    # -- the pull request's link to the issue

    def branch_tag(self, key: Key) -> str:
        """What a job's branch name carries of the issue, before the job id. Empty for nothing."""
        ...

    def pr_title_problem(self, title: str, key: Key) -> str | None:
        """What is wrong with a pull request title, or None."""
        ...

    def pr_link_rule(self) -> str:
        """What the build agent is told about linking the issue from the pull request body."""
        ...

    def pr_link_problem(self, body: str, key: Key) -> str | None:
        """What is wrong with the link in a pull request body, or None."""
        ...

    # -- install, doctor and remove

    def install(self, say) -> None:
        """Create what the tracker needs on the remote side."""
        ...

    def doctor(self, ok, warn, fail) -> None:
        """Report whether the tracker is ready, one line per finding."""
        ...

    def uninstall(self, say) -> None:
        """Delete what `install` created."""
        ...


class OneTracker:
    """What a single tracker shares with `routing.Trackers`, so a call site is written once for one or two.

    A project with one tracker uses it as it is: `of` is the tracker itself, and nothing is unread.
    """

    kind: str
    has_board: bool = False

    def of(self, key: Key) -> Any:
        """The tracker that holds `key`: this one, unless the key's shape says it is another tracker's."""
        owner: str | None = key_kind(key)
        if owner is not None and owner != self.kind:
            raise Refused(left_kind(key, owner))
        return self

    @property
    def members(self) -> list[Any]:
        return [self]

    @property
    def primary(self) -> Any:
        return self

    def member(self, kind: str) -> Any:
        if kind != self.kind:
            raise Refused(f"this project takes no jobs from {LABELS.get(kind, kind)}: [tracker].kind is {self.kind}")
        return self

    def board_member(self) -> Any:
        return self if self.has_board else None

    @property
    def problems(self) -> list[str]:
        """The trackers that could not be read. One tracker raises instead, so this stays empty."""
        return []

    @property
    def unread(self) -> set[str]:
        return set()

    def reason(self, kind: str) -> str:
        return ""

    def failed(self, tracker: Any, what: object, error: Exception) -> str:
        """One tracker whose call for `what` fails stops the command, as it always has."""
        raise error
