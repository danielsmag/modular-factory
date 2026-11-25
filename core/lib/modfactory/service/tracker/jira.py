"""A Jira kanban board as the tracker: the queue is a column, and each step's card sits in a status.

People reply in Jira comments. Code, branches and pull requests stay on GitHub. Which status a step
maps to is read from `[tracker.jira.columns]` each time it is needed, so editing that table is all a
changed board takes.
"""

import re
from typing import Any

from ... import board
from ...util import Refused
from . import adf
from .base import Key, Who
from .jira_api import Client

KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[1-9][0-9]*")
# The last line of every comment the factory writes. The factory posts from the operator's own account,
# so this is what tells its comments from ones the operator typed. It is plain text, so it survives
# whichever way Jira is reached.
SIGNATURE = "— mfactory"
FIELDS = "summary,description,status,reporter,assignee,labels,created,updated"
LIST_FIELDS = "summary,status,reporter,assignee,labels,created,updated"
# Issue keys in one JQL `key in (...)`.
KEYS_PER_CALL = 50
# Cards read from the board to find one in each status, for checking the workflow's transitions.
SAMPLE_CARDS = 500


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


def _text(value: object) -> str:
    """A description or comment body as text: a document, or text a tool already flattened."""
    return value if isinstance(value, str) else adf.to_text(value)


def _status_name(value: object) -> str:
    """A status as Jira gives it, an object with a name, or as a tool may flatten it, the name alone."""
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return value if isinstance(value, str) else ""


class JiraTracker:
    kind = "jira"
    label = "Jira"
    # How Jira is reached: its REST API here, the Atlassian CLI in `jira_acli.py`.
    via = "rest"
    # True when the tracker can tell who moved a card, so a move can answer a human step.
    drag_answers = True
    # Asks and replies are in Jira. A comment on the pull request does not count.
    pr_threads = False
    comment_limit = 30000
    # Jira's development panel finds a pull request by the issue key in its title.
    links_by_title = True

    def __init__(self, settings: dict[str, Any], client: Client | None = None,
                 steps: dict[str, Any] | None = None, start: str = "", escalate: str = ""):
        # The pipeline, for reports that compare the board with it.
        self.steps: dict[str, Any] = steps or {}
        self.start: str = start
        self.escalate: str = escalate
        self.board: int = settings["board"]
        self.jql: str = str(settings.get("jql") or "").strip()
        self.columns: dict[str, Any] = settings.get("columns") or {}
        self.issue_type: str = settings.get("issue_type") or "Task"
        self.maintainers: list[str] = list(settings.get("maintainers") or [])
        self.project: str = str(settings.get("project") or "")
        self.site: str = str(settings["site"]).rstrip("/")
        self.api = client or Client(self.site, settings["email_env"], settings["token_env"])
        self._operator: str | None = None
        self._names: dict[str, str] = {}
        self._writers: dict[str, bool] = {}
        self._project: str | None = None

    @classmethod
    def from_config(cls, cfg, gh=None) -> "JiraTracker":
        return cls(cfg["tracker"]["jira"], steps=cfg.steps, start=cfg["run"]["start"],
                   escalate=cfg["run"]["escalate_to"])

    # -- keys

    def key(self, ref: object) -> Key:
        text: str = str(ref).strip()
        if not KEY.fullmatch(text):
            raise Refused(f"{text!r} is not a Jira issue key, such as PROJ-123")
        return text.upper()

    def ref(self, key: Key) -> str:
        return str(key)

    def _url(self, key: Key) -> str:
        return f"{self.site}/browse/{key}"

    # -- reading Jira's shapes

    def _person(self, user: object) -> dict[str, str]:
        """An account as `{"login": account id, "name": shown name}`. The id is what jobs store and compare."""
        if not isinstance(user, dict):
            return {"login": "", "name": user if isinstance(user, str) else ""}
        login: str = str(user.get("accountId") or "")
        name: str = str(user.get("displayName") or login)
        if login:
            self._names[login] = name
        return {"login": login, "name": name}

    def _issue(self, raw: dict[str, Any]) -> dict[str, Any]:
        fields: dict[str, Any] = raw.get("fields") or {}
        status: object = fields.get("status")
        category: object = (status.get("statusCategory") or {}).get("key") if isinstance(status, dict) else None
        assignee: object = fields.get("assignee")
        return {
            "number": raw["key"], "title": fields.get("summary") or "", "url": self._url(raw["key"]),
            "body": _text(fields.get("description")), "state": "CLOSED" if category == "done" else "OPEN",
            "status": _status_name(status), "author": self._person(fields.get("reporter")),
            "assignees": [self._person(assignee)] if assignee else [],
            "labels": [{"name": str(name)} for name in fields.get("labels") or []],
            "createdAt": fields.get("created"), "updatedAt": fields.get("updated"),
        }

    # -- issues and the queue

    def issue(self, key: Key) -> dict[str, Any]:
        found: dict[str, Any] = self._issue(self.api.get(f"/rest/api/3/issue/{key}", fields=FIELDS))
        found["comments"] = self.comments(key)
        return found

    def issue_details(self, key: Key) -> object:
        return self.issue(key)

    def _in_queue(self) -> str:
        names: list[str] = board.queue_statuses(self.columns)
        quoted: str = ", ".join('"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"' for name in names)
        return f"status in ({quoted})"

    def _board_issues(self, clause: str) -> list[dict[str, Any]]:
        """The board's cards that match `clause` and the project's own `jql`. The board adds its saved filter."""
        jql: str = f"{clause} AND ({self.jql})" if self.jql else clause
        rows: list[Any] = self.api.pages("GET", f"/rest/agile/1.0/board/{self.board}/issue", "issues",
                                         jql=jql, fields=LIST_FIELDS)
        return [self._issue(row) for row in rows]

    def queue(self) -> list[dict[str, Any]]:
        """The cards in the queue column, in the board's order: the top card is built first."""
        return self._board_issues(self._in_queue())

    def queue_gate(self, issue: dict[str, Any]) -> str | None:
        key: Key = issue["number"]
        names: list[str] = board.queue_statuses(self.columns)
        shown: str = ", ".join(f"`{name}`" for name in names)
        if not any(_same(issue.get("status") or "", name) for name in names):
            return (f"{key} is in `{issue.get('status')}`, not in the queue column ({shown}). "
                    "Only cards there run.")
        if not self._board_issues(f'key = "{key}" AND {self._in_queue()}'):
            scope: str = f"board {self.board}" + (f" with `{self.jql}`" if self.jql else "")
            return f"{key} is in the queue column, but {scope} does not show it. Only the board's cards run."
        return None

    def withdrawn(self, key: Key, queued: set[Key]) -> str | None:
        """A running job's card has left the queue column for its step's column, which is no reason to stop."""
        return None

    def queue_note(self) -> str:
        shown: str = ", ".join(f"`{name}`" for name in board.queue_statuses(self.columns))
        return f"the queue is {shown} on board {self.board}"

    def empty_note(self) -> str:
        shown: str = ", ".join(f"`{name}`" for name in board.queue_statuses(self.columns))
        return f"no card is in {shown} on board {self.board}"

    def enqueue_hint(self) -> str:
        return f"Move a card to `{board.queue_statuses(self.columns)[0]}` on board {self.board}"

    def requeue_hint(self) -> str:
        return f"move its card to `{board.queue_statuses(self.columns)[0]}` to run it again"

    def dequeue(self, key: Key) -> None:
        """Nothing to do: a rejected job's card has left the queue column."""

    def _project_key(self) -> str:
        if self.project:
            return self.project
        if self._project is None:
            found: object = self.api.get(f"/rest/agile/1.0/board/{self.board}")
            location: object = found.get("location") if isinstance(found, dict) else None
            project: object = location.get("projectKey") if isinstance(location, dict) else None
            if not isinstance(project, str) or not project:
                raise Refused(f"board {self.board} is not in a Jira project, so the factory cannot file an issue")
            self._project = project
        return self._project

    def create_issue(self, title: str, body: str, queue: bool = False) -> str:
        created: dict[str, Any] = self.api.post("/rest/api/3/issue", {"fields": {
            "project": {"key": self._project_key()}, "summary": title,
            "description": adf.from_markdown(body), "issuetype": {"name": self.issue_type}}})
        key: str = created["key"]
        if queue:
            target: str = board.queue_statuses(self.columns)[0]
            try:
                self._to_status(key, target, f"[tracker.jira.columns].{board.QUEUE}")
            except Refused as error:
                raise Refused(f"{key} was created, but its card is not in the queue: {error}") from None
        return self._url(key)

    def close_issue(self, key: Key, reason: str = "completed") -> bool:
        """Move the card to the status of `done`, or of `rejected` for `not planned`. False when it is there."""
        state: str = "rejected" if reason == "not planned" else "done"
        status: str | None = board.status_for(self.columns, state)
        if status is None:
            raise Refused(f"[tracker.jira.columns] has no status for `{state}`, so move {key} on the board yourself")
        return self._to_status(key, status, f"[tracker.jira.columns].{state}")

    # -- comments

    def comments(self, key: Key) -> list[dict[str, Any]]:
        return self._comments_from(
            self.api.pages("GET", f"/rest/api/3/issue/{key}/comment", "comments", orderBy="created"))

    def _comments_from(self, rows: list[Any]) -> list[dict[str, Any]]:
        """Comments as Jira lists them, each saying whether the factory wrote it.

        A factory comment is one the operator's account posted that ends with SIGNATURE. The account
        is looked up only when a comment is signed.
        """
        out: list[dict[str, Any]] = []
        signed: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            comment: dict[str, Any] = {
                "id": str(row.get("id")), "author": self._person(row.get("author")),
                "body": _text(row.get("body")), "createdAt": row.get("created"), "factory": False}
            out.append(comment)
            if comment["body"].rstrip().endswith(SIGNATURE):
                signed.append(comment)
        operator: str = self.operator() if signed else ""
        for comment in signed:
            if comment["author"]["login"] == operator:
                comment["factory"] = True
                comment["body"] = comment["body"].rstrip()[:-len(SIGNATURE)].rstrip()
        return out

    def _document(self, body: str, factory: bool) -> dict[str, Any]:
        """A comment's body as Jira stores it. A factory comment ends with SIGNATURE."""
        return adf.from_markdown(f"{body.rstrip()}\n\n{SIGNATURE}" if factory else body)

    def comment(self, key: Key, body: str, factory: bool = True) -> None:
        self.api.post(f"/rest/api/3/issue/{key}/comment", {"body": self._document(body, factory)})

    # -- people

    def operator(self) -> str:
        """The Jira account the token belongs to."""
        if self._operator is None:
            self._operator = self._person(self.api.get("/rest/api/3/myself"))["login"]
        return self._operator

    def mention(self, login: str, name: str | None = None) -> str:
        """A person as readers know them: by name. An account Jira will not show keeps its id."""
        if name:
            return name
        if login not in self._names:
            try:
                self._person(self.api.get("/rest/api/3/user", accountId=login))
            except Refused:
                self._names[login] = login
        return self._names.get(login, login)

    def can_write(self, login: str, key: Key) -> bool:
        """True for the operator, and for `maintainers` when set, else for anyone the issue can be assigned to."""
        if login == self.operator():
            return True
        if self.maintainers:
            return login in self.maintainers
        if login not in self._writers:
            found: object = self.api.get("/rest/api/3/user/assignable/search", issueKey=key, accountId=login)
            self._writers[login] = isinstance(found, list) and any(
                isinstance(user, dict) and user.get("accountId") == login for user in found)
        return self._writers[login]

    # -- waiting for a person: the card's column says it

    def waiting_on(self, key: Key, who: Who) -> str | None:
        return None

    def waiting_off(self, key: Key, flag: str | None) -> None:
        return None

    # -- the card

    def move(self, key: Key, step: str) -> None:
        """Put the card in the status of `step`. A step with no status leaves the card where it is."""
        status: str | None = board.status_for(self.columns, step)
        if status is not None:
            self._to_status(key, status, f"[tracker.jira.columns].{step}")

    def status_for(self, step: str) -> str | None:
        return board.status_for(self.columns, step)

    def statuses(self, keys: list[Key]) -> dict[Key, str]:
        """Where each card is. Several cards are read from the board in one call; one is read on its own."""
        wanted: list[Key] = list(dict.fromkeys(keys))
        found: dict[Key, str] = {}
        for start in range(0, len(wanted) if len(wanted) > 1 else 0, KEYS_PER_CALL):
            quoted: str = ", ".join(f'"{key}"' for key in wanted[start:start + KEYS_PER_CALL])
            for row in self.api.pages("GET", f"/rest/agile/1.0/board/{self.board}/issue", "issues",
                                      jql=f"key in ({quoted})", fields="status"):
                found[row["key"]] = ((row.get("fields") or {}).get("status") or {}).get("name") or ""
        for key in wanted:
            if key in found:
                continue
            try:  # a card in a status that is on no column of the board, or the only card asked for
                raw: dict[str, Any] = self.api.get(f"/rest/api/3/issue/{key}", fields="status")
            except Refused:
                continue  # deleted, or hidden from this account: unknown
            found[key] = ((raw.get("fields") or {}).get("status") or {}).get("name") or ""
        return {key: status for key, status in found.items() if status}

    def last_move(self, key: Key, status: str) -> dict[str, Any] | None:
        """The newest status change into `status`, from the issue's history."""
        entries: list[Any] = self.api.pages("GET", f"/rest/api/3/issue/{key}/changelog", "values")
        for entry in reversed(entries):
            for item in entry.get("items") or []:
                if item.get("field") == "status" and _same(item.get("toString") or "", status):
                    return {"id": str(entry.get("id")), "by": self._person(entry.get("author")),
                            "from": item.get("fromString") or "", "to": item.get("toString") or status}
        return None

    def _to_status(self, key: Key, status: str, setting: str) -> bool:
        """Transition the issue to `status`. False when it is already there."""
        found: dict[str, Any] = self.api.get(f"/rest/api/3/issue/{key}", fields="status", expand="transitions")
        current: str = ((found.get("fields") or {}).get("status") or {}).get("name") or ""
        if _same(current, status):
            return False
        for transition in found.get("transitions") or []:
            if _same((transition.get("to") or {}).get("name") or "", status):
                self.api.post(f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": transition["id"]}})
                return True
        raise Refused(f"Jira has no transition for {key} from `{current}` to `{status}` ({setting}). "
                      "Allow it in the Jira workflow, or map the step to another status.")

    # -- the pull request's link to the issue

    def branch_tag(self, key: Key) -> str:
        """What a job's branch name starts with, so Jira's development panel finds the branch."""
        return f"{key}-"

    def pr_link_rule(self) -> str:
        return ("The first line is `Refs <issue key>`, with the key from `Issue:` in your prompt, such as "
                "`Refs PROJ-123`. The issue is in Jira, so do not write `#` before it. "
                "Put the same key in the pull request title, such as `Add dark mode [PROJ-123]`: "
                "Jira links the pull request by it.")

    def pr_link_problem(self, body: str, key: Key) -> str | None:
        lines: list[str] = body.strip().splitlines()
        if not lines or lines[0].strip() != f"Refs {key}":
            return f"body must start with `Refs {key}`, the Jira issue this job builds."
        return None

    def pr_title_problem(self, title: str, key: Key) -> str | None:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(str(key))}(?![0-9])", title):
            return None
        return f"title must contain {key}, so Jira links the pull request to the issue."

    # -- install, doctor and remove

    def install(self, say) -> None:
        """Jira needs nothing created: the board's columns are the queue and the steps."""

    def board_report(self) -> dict[str, Any]:
        """The mapping against the live board: a row per step, what is wrong, and what could not be checked."""
        layout: dict[str, Any] = self.api.get(f"/rest/agile/1.0/board/{self.board}/configuration")
        statuses: list[Any] = [s for s in self.api.get("/rest/api/3/status") if isinstance(s, dict)]
        names: dict[str, str] = {str(s.get("id")): str(s.get("name")) for s in statuses}
        finished: set[str] = {str(s.get("name")).casefold() for s in statuses
                              if (s.get("statusCategory") or {}).get("key") == "done"}
        warnings: list[str] = []
        column_of: dict[str, str] = {}
        on_board: list[str] = []
        for column in (layout.get("columnConfig") or {}).get("columns") or []:
            for status in column.get("statuses") or []:
                name: str | None = names.get(str(status.get("id")))
                if name and name.casefold() not in column_of:
                    column_of[name.casefold()] = str(column.get("name"))
                    on_board.append(name)
        known: set[str] = {name.casefold() for name in names.values()}
        used: set[str] = set()

        def row(step: str, kind: str, enabled: bool, status: str | None) -> dict[str, Any]:
            column: str | None = None
            problem: str | None = None
            if status is not None:
                used.add(status.casefold())
                column = column_of.get(status.casefold())
                if status.casefold() not in known:
                    problem = f"Jira has no status `{status}`"
                elif column is None:
                    problem = f"`{status}` is on no column of board {self.board}, so the card leaves the board"
                elif status.casefold() in finished and step == board.QUEUE:
                    problem = (f"`{status}` is in Jira's Done category, so a card there counts as a closed issue "
                               "and no job starts from it")
                elif status.casefold() in finished and kind != "end":
                    warnings.append(f"[tracker.jira.columns].{step}: `{status}` is in Jira's Done category, so Jira "
                                    f"counts the issue as finished while the job is at {step}")
            return {"step": step, "type": kind, "enabled": enabled, "status": status, "column": column,
                    "problem": problem}

        queue: list[str] = board.queue_statuses(self.columns)
        rows: list[dict[str, Any]] = [row(board.QUEUE, "-", True, status) for status in queue]
        rows += [row(name, step["type"], step.get("enabled", True), self.status_for(name))
                 for name, step in self.steps.items()]
        rows += [row(state, "end", True, self.status_for(state)) for state in ("done", "rejected", "failed")]
        sound: set[str] = {r["status"].casefold() for r in rows if r["status"] and not r["problem"]}
        problems, unchecked = self._workflow_gaps(queue, sound)
        return {"tracker": self.kind, "via": self.via, "site": self.site, "board": self.board, "rows": rows,
                "unused": [name for name in on_board if name.casefold() not in used],
                "problems": problems, "unchecked": unchecked, "warnings": warnings, "note": None}

    def _workflow_gaps(self, queue: list[str], sound: set[str]) -> tuple[list[str], list[str]]:
        """Moves the pipeline needs and the workflow lacks, and the statuses no card could show them for.

        Jira lists an issue's transitions, not a workflow's, so each status is checked through one card
        that is in it now.
        """
        needed: dict[str, list[tuple[str, str, str]]] = {}
        for a, b, why in board.transitions(self.steps, self.status_for, queue, self.start, self.escalate):
            if a.casefold() in sound and b.casefold() in sound:
                needed.setdefault(a, []).append((a, b, why))
        if not needed:
            return [], []
        sample: dict[str, str] = {}
        for card in self.api.pages("GET", f"/rest/agile/1.0/board/{self.board}/issue", "issues",
                                   limit=SAMPLE_CARDS, fields="status"):
            where: str = ((card.get("fields") or {}).get("status") or {}).get("name") or ""
            sample.setdefault(where.casefold(), card["key"])
        problems: list[str] = []
        unchecked: list[str] = []
        for status, moves in needed.items():
            key: str | None = sample.get(status.casefold())
            if key is None:
                unchecked.append(status)
                continue
            found: dict[str, Any] = self.api.get(f"/rest/api/3/issue/{key}", fields="status", expand="transitions")
            allowed: set[str] = {str((t.get("to") or {}).get("name") or "").casefold()
                                 for t in found.get("transitions") or []}
            problems += [f"the workflow has no transition from `{a}` to `{b}` ({why})"
                         for a, b, why in moves if b.casefold() not in allowed]
        return problems, unchecked

    def doctor(self, ok, warn, fail) -> None:
        try:
            ok(f"Jira: signed in to {self.site} as {self.mention(self.operator())}")
            report: dict[str, Any] = self.board_report()
        except Refused as error:
            fail(str(error))
            return
        bad: list[dict[str, Any]] = [r for r in report["rows"] if r["problem"]]
        for found in bad:
            fail(f"[tracker.jira.columns].{found['step']}: {found['problem']}")
        if not bad:
            ok(f"Jira board {self.board}: every mapped status is on the board")
        for problem in report["problems"]:
            fail(f"Jira: {problem}")
        for warning in report["warnings"]:
            warn(warning)
        if report["unchecked"]:
            warn(f"Jira: transitions not checked from {', '.join(report['unchecked'])}: "
                 "no card is in those statuses now")
        for name, step in self.steps.items():
            if step.get("type") != "human" or not step.get("enabled", True):
                continue
            if self.status_for(name) is None:
                warn(f"[steps.{name}] has no status in [tracker.jira.columns], so it is answered by comment only")
            for status, outcomes in board.drags(self.steps, self.status_for, name).items():
                if len(outcomes) > 1:
                    warn(f"[steps.{name}]: moving the card to `{status}` could mean {' or '.join(outcomes)}, "
                         "so it is answered by comment only")

    def uninstall(self, say) -> None:
        """Nothing was created in Jira, so nothing is deleted."""
