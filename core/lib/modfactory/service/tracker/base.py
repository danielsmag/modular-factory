"""What the factory asks of a tracker: the place jobs come from and people reply in."""

from typing import Any, Protocol

# An issue's key, as the tracker names it: a number on GitHub.
Key = int | str
# Who a human step waits for: "author", "maintainer", "operator", or a list of logins.
Who = str | list[str]


class Tracker(Protocol):
    """One tracker. The engine calls only these, so it works the same for any of them.

    An issue is a dict with `number` (its key), `title`, `body`, `state` (`OPEN` or not), `url`,
    `author` (`{"login": ...}`), `labels`, and `comments`. A comment has `id`, `author`, `body`,
    `createdAt`, and `factory`: True when the factory wrote it.
    """

    kind: str

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
        """The open issues waiting for a job."""
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

    def can_write(self, login: str) -> bool:
        """True for the operator and for anyone who may answer a maintainer step."""
        ...

    # -- waiting for a person

    def waiting_on(self, key: Key, who: Who) -> str | None:
        """Show on the issue that a person is asked. Returns what `waiting_off` needs to undo it."""
        ...

    def waiting_off(self, key: Key, flag: str | None) -> None: ...

    # -- the pull request's link to the issue

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
