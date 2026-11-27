"""What the Board's Jira and GitHub blocks show: boards, their cards, and a repository's open issues.

Each reader takes its Jira `Client` or `GitHub` as an argument and returns plain data, with every string
redacted and only web addresses kept as links. Nothing here knows about HTTP: the dashboard's server
builds the clients from each project's settings and adapts the answers, and a server for a team could
do the same with each viewer's own access.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

from .. import activity
from ..service.gh import GitHub
from ..service.tracker import jira
from ..service.tracker.jira_api import Client
from ..util import FactoryError

# A link the page may follow: a web address, or a server on this machine such as a local Jira.
WEB_URL: re.Pattern[str] = re.compile(r"https://\S+|http://(?:127\.0\.0\.1|localhost)(?::\d+)?(?:/\S*)?")
# How long a fresh read is reused when the page asks for one anyway.
FRESH_AFTER_SECONDS: float = 5.0


def web_url(value: object) -> str | None:
    return value if isinstance(value, str) and WEB_URL.fullmatch(value) else None


def text(value: object) -> str | None:
    """A string from a tracker, redacted like agent activity. None for anything else."""
    return activity.redact(value) if isinstance(value, str) else None


def board_url(site: str, board_id: int) -> str | None:
    """Where a board opens in Jira."""
    return web_url(f"{site.rstrip('/')}/secure/RapidBoard.jspa?rapidView={board_id}")


# ---- Jira -------------------------------------------------------------------------


def jira_boards(client: Client, configured: int, limit: int = 500) -> dict[str, Any]:
    """The boards the account may see, for the board picker. `configured` is the project's own board."""
    found: list[dict[str, Any]] = jira.boards(client, limit=limit)
    return {
        "site": web_url(client.site), "configured": configured or None, "note": None,
        "boards": [{"id": board["id"], "name": text(board["name"]) or "", "type": text(board["type"]) or "",
                    "project": text(board["project"]) or "", "url": board_url(client.site, board["id"])}
                   for board in found],
    }


def jira_board(client: Client, board_id: int, days: int = 14, limit: int = 300) -> dict[str, Any]:
    """One board's columns and cards, as `jira.board_view` reads them, ready for the page."""
    view: dict[str, Any] = jira.board_view(client, board_id, days=days, limit=limit)
    board: dict[str, Any] = view["board"]
    return {
        "board": {"id": board["id"], "name": text(board["name"]) or "", "type": text(board["type"]) or "",
                  "url": board_url(client.site, board["id"])},
        "columns": [{"name": text(column["name"]) or "", "cards": [_card(card) for card in column["cards"]]}
                    for column in view["columns"]],
        "shown": view["shown"], "total": view["total"], "hidden": view["hidden"], "truncated": view["truncated"],
    }


def _card(card: dict[str, Any]) -> dict[str, Any]:
    return {"key": text(card["key"]) or "", "title": text(card["title"]) or "", "url": web_url(card["url"]),
            "status": text(card["status"]), "type": text(card["type"]), "priority": text(card["priority"]),
            "assignee": text(card["assignee"]), "labels": [text(label) or "" for label in card["labels"]],
            "updated_at": text(card["updated_at"])}


# ---- GitHub -----------------------------------------------------------------------


def github_issues(gh: GitHub, me: Callable[[], str], repo_url: Callable[[], str | None] | None = None,
                  limit: int = 200) -> dict[str, Any]:
    """A repository's open issues, most recently updated first, and who `me` is for "Assigned to me"."""
    found: list[dict[str, Any]] = gh.open_issues(limit=limit + 1)
    return {
        "repo_url": web_url((repo_url or gh.repo_url)()), "me": text(me()),
        "issues": [_issue(issue) for issue in found[:limit]], "truncated": len(found) > limit,
    }


def _login(value: object) -> str | None:
    return text(value.get("login")) if isinstance(value, dict) else None


def _issue(issue: dict[str, Any]) -> dict[str, Any]:
    number: object = issue.get("number")
    return {
        "number": number if isinstance(number, int) and not isinstance(number, bool) else None,
        "title": text(issue.get("title")) or "", "url": web_url(issue.get("url")),
        "author": _login(issue.get("author")),
        "assignees": [login for login in map(_login, issue.get("assignees") or []) if login],
        "labels": [name for name in (text(label.get("name")) for label in issue.get("labels") or []
                                     if isinstance(label, dict)) if name],
        "created_at": text(issue.get("createdAt")), "updated_at": text(issue.get("updatedAt")),
    }


# ---- The cache --------------------------------------------------------------------


class FeedCache:
    """Answers reused for `ttl` seconds, and failures for `error_ttl`, so the open page does not hammer a tracker.

    Each key has its own lock: a slow Jira board never holds up the GitHub list, and two requests for the
    same board wait for one read. When full, the entry used longest ago goes first.
    """

    def __init__(self, ttl: float, error_ttl: float, size: int = 64,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl: float = ttl
        self._error_ttl: float = error_ttl
        self._size: int = size
        self._clock: Callable[[], float] = clock
        self._entries: OrderedDict[Hashable, tuple[float, bool, Any]] = OrderedDict()
        self._locks: dict[Hashable, threading.Lock] = {}
        self._guard: threading.Lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: Hashable, load: Callable[[], Any], fresh: bool = False) -> Any:
        """The answer for `key`, read again when it is old, or when `fresh` and it is over 5 s old."""
        with self._guard:
            lock: threading.Lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                entry: tuple[float, bool, Any] | None = self._entries.get(key)
                if entry is not None:
                    self._entries.move_to_end(key)
            if entry is not None:
                at, ok, value = entry
                age: float = self._clock() - at
                if age < (self._ttl if ok else self._error_ttl) and not (fresh and age >= FRESH_AFTER_SECONDS):
                    if ok:
                        return value
                    raise value
            try:
                value = load()
            except FactoryError as error:
                self._keep(key, False, error)
                raise
            self._keep(key, True, value)
            return value

    def _keep(self, key: Hashable, ok: bool, value: Any) -> None:
        with self._guard:
            self._entries[key] = (self._clock(), ok, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._size:
                self._entries.popitem(last=False)
