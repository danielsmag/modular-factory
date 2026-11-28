"""Jira through the Atlassian CLI, `acli`: the fallback when the REST credentials are not in the environment.

`acli` signs in on its own (`acli jira auth login`), so no token is exported. It reaches less of Jira than
REST does. It has no board, so the queue is scoped by `[tracker.jira].project`; it has no issue history, so
a moved card cannot answer a step; and it cannot say who may be assigned an issue, so a maintainer is the
operator or an account in `[tracker.jira].maintainers`.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

from ... import board, config
from ...util import Refused, run
from . import adf
from .base import RELATION_TEXT, Key
from .jira import KEY, KEYS_PER_CALL, JiraTracker, _same, _status_name
from .jira_api import rest_ready

# Fields asked of `workitem search`. The second set is its documented default, for a CLI that refuses more.
SEARCH_FIELDS = ("key,summary,status,assignee,reporter,labels,created,updated", "key,summary,status,assignee")
VIEW_FIELDS = "summary,description,status,reporter,assignee,labels,created,updated,comment"
INSTALL = "https://developer.atlassian.com/cloud/acli/guides/install-acli/"
TIMEOUT_SECONDS = 120
LIMITS = ("acli cannot read a board's columns or a workflow's transitions. "
          "Export JIRA_EMAIL and JIRA_API_TOKEN to check them.")


def parse(text: str) -> Any:
    """The JSON a command printed, skipping any line of other output before it."""
    starts: list[int] = [i for i in (text.find("["), text.find("{")) if i >= 0]
    for start in sorted(starts):
        try:
            return json.loads(text[start:])
        except ValueError:
            continue
    raise Refused(f"acli did not print JSON: {text.strip()[:200] or 'nothing'}")


def items(found: object, key: str) -> list[Any]:
    """A list of results, whether acli printed the list itself or an object that holds it."""
    if isinstance(found, list):
        return found
    if isinstance(found, dict):
        for name in (key, "values", "items", "results"):
            if isinstance(found.get(name), list):
                return found[name]
    return []


def from_config(cfg, gh=None) -> JiraTracker:
    """The Jira tracker `[tracker.jira].via` asks for.

    `auto` uses REST while the email and the token are both found, and falls back to acli when they are
    not and acli is installed. A `token_file` that is set but unusable is an error, not a fallback.
    With neither credentials nor acli, it is REST, whose first request says what is missing.
    """
    settings: dict[str, Any] = cfg["tracker"]["jira"]
    via: str = settings.get("via", "auto")
    ready: bool = via != "acli" and rest_ready(settings, cfg.repo)
    use_acli: bool = via == "acli" or (via == "auto" and not ready and shutil.which("acli") is not None)
    cls: type[JiraTracker] = AcliJiraTracker if use_acli else JiraTracker
    return cls(settings, steps=config.all_steps(cfg), start=cfg["run"]["start"], escalate=cfg["run"]["escalate_to"],
               repo=cfg.repo)


class AcliJiraTracker(JiraTracker):
    via = "acli"
    links_both_ways = False
    # acli shows no issue history, so who moved a card is unknown.
    drag_answers = False

    # -- running acli

    def _acli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            res: subprocess.CompletedProcess[str] = run(["acli", *args], check=False, timeout=TIMEOUT_SECONDS)
        except FileNotFoundError:
            raise Refused(f"the Atlassian CLI `acli` isn't installed: {INSTALL}") from None
        except subprocess.TimeoutExpired:
            raise Refused(f"`acli {' '.join(args[:3])}` did not answer in {TIMEOUT_SECONDS} seconds") from None
        if check and res.returncode != 0:
            raise Refused(self._failure(args, res))
        return res

    @staticmethod
    def _said(res: subprocess.CompletedProcess[str]) -> str:
        lines: list[str] = (res.stderr or res.stdout).strip().splitlines()
        return lines[-1].strip() if lines else f"exit code {res.returncode}"

    def _failure(self, args: tuple[str, ...], res: subprocess.CompletedProcess[str]) -> str:
        said: str = self._said(res)
        hint: str = " Sign in with `acli jira auth login`." if re.search(
            r"(?i)unauthori[sz]ed|not logged in|not authenticated", said) else ""
        return f"`acli {' '.join(args[:3])}` failed: {said}{hint}"

    def _search(self, jql: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Work items that match `jql`, as `{"key": ..., "fields": {...}}`."""
        paging: list[str] = ["--limit", str(limit)] if limit else ["--paginate"]
        res: subprocess.CompletedProcess[str] | None = None
        args: tuple[str, ...] = ()
        for fields in SEARCH_FIELDS:
            args = ("jira", "workitem", "search", "--jql", jql, "--fields", fields, "--json", *paging)
            res = self._acli(*args, check=False)
            if res.returncode == 0:
                break
        assert res is not None
        if res.returncode != 0:
            raise Refused(self._failure(args, res))
        try:
            found: object = parse(res.stdout)
        except Refused:
            return []  # a search with no result may print a sentence and no JSON
        return [row for row in items(found, "issues") if isinstance(row, dict) and row.get("key")]

    def _view(self, key: Key, fields: str) -> dict[str, Any]:
        found: object = parse(self._acli("jira", "workitem", "view", str(key), "--json", "--fields", fields).stdout)
        if not isinstance(found, dict):
            raise Refused(f"acli did not describe {key}")
        found.setdefault("key", str(key))
        return found

    def _with_file(self, document: dict[str, Any], *args: str, flag: str) -> subprocess.CompletedProcess[str]:
        """Run a command that reads a Jira document from a file."""
        handle, path = tempfile.mkstemp(prefix="mfactory-", suffix=".json")
        try:
            with os.fdopen(handle, "w") as out:
                json.dump(document, out)
            return self._acli(*args, flag, path)
        finally:
            os.unlink(path)

    # -- issues and the queue

    def _scope(self) -> str:
        if not self.project:
            raise Refused(f"acli cannot read the filter of board {self.board}, so set [tracker.jira].project "
                          "to the project's key: it keeps other projects' cards out of the queue")
        return f'project = "{self.project}"'

    def _board_issues(self, clause: str) -> list[dict[str, Any]]:
        """The project's work items that match `clause` and the project's own `jql`, oldest first."""
        jql: str = f"{self._scope()} AND {clause}" + (f" AND ({self.jql})" if self.jql else "")
        return [self._issue(row) for row in self._search(f"{jql} ORDER BY created ASC")]

    def _project_key(self) -> str:
        self._scope()
        return self.project

    def issue(self, key: Key) -> dict[str, Any]:
        raw: dict[str, Any] = self._view(key, VIEW_FIELDS)
        found: dict[str, Any] = self._issue(raw)
        found["comments"] = self._comments_from(self._comment_rows(key, raw))
        return found

    def _comment_rows(self, key: Key, raw: dict[str, Any]) -> list[Any]:
        """The issue's comments from `view`, or from `comment list` when `view` gave only the first page."""
        held: object = (raw.get("fields") or {}).get("comment")
        rows: list[Any] = items(held, "comments")
        total: object = held.get("total") if isinstance(held, dict) else None
        if isinstance(total, int) and total > len(rows):
            listed: object = parse(self._acli("jira", "workitem", "comment", "list", "--key", str(key),
                                              "--json", "--paginate").stdout)
            rows = items(listed, "comments")
        return rows

    def comments(self, key: Key) -> list[dict[str, Any]]:
        return self._comments_from(self._comment_rows(key, self._view(key, "comment")))

    def comment(self, key: Key, body: str, factory: bool = True) -> None:
        self._with_file(self._document(body, factory), "jira", "workitem", "comment", "create",
                        "--key", str(key), flag="--body-file")

    def link(self, key: Key, other: str, relation: str) -> None:
        """acli's link command is not one the factory relies on, so the relation is said in a comment."""
        self.comment(key, f"{RELATION_TEXT[relation]} {other}")

    def create_issue(self, title: str, body: str, queue: bool = False) -> str:
        res = self._with_file(adf.from_markdown(body), "jira", "workitem", "create", "--summary", title,
                              "--project", self._project_key(), "--type", self.issue_type, "--json",
                              flag="--description-file")
        try:
            found: object = parse(res.stdout)
        except Refused:
            found = None
        key: object = found.get("key") if isinstance(found, dict) else None
        if not isinstance(key, str):
            named: re.Match[str] | None = KEY.search(res.stdout)
            if named is None:
                raise Refused(f"acli created an issue and did not say its key: {res.stdout.strip()[:200]}")
            key = named.group()
        if queue:
            try:
                self._to_status(key, board.queue_statuses(self.columns)[0], f"[tracker.jira.columns].{board.QUEUE}")
            except Refused as error:
                raise Refused(f"{key} was created, but its card is not in the queue: {error}") from None
        return self._url(key)

    # -- people

    def operator(self) -> str:
        """The account acli is signed in as, read from a work item it is assigned or reported on.

        acli does not print the account's id, and a job stores accounts by id.
        """
        if self._operator is None:
            for role in ("assignee", "reporter"):
                rows: list[dict[str, Any]] = self._search(f"{role} = currentUser() ORDER BY updated DESC", limit=1)
                if not rows:
                    continue
                held: object = (rows[0].get("fields") or {}).get(role)
                if not (isinstance(held, dict) and held.get("accountId")):
                    held = (self._view(rows[0]["key"], role).get("fields") or {}).get(role)
                login: str = self._person(held)["login"]
                if login:
                    self._operator = login
                    break
            else:
                raise Refused("acli cannot say which Jira account it is signed in as, and this account has no "
                              "issue assigned to it or reported by it to read that from. Assign it one issue, "
                              "or export JIRA_EMAIL and JIRA_API_TOKEN so the factory uses Jira's REST API.")
        return self._operator

    def mention(self, login: str, name: str | None = None) -> str:
        """A person by name when acli has shown one. It has no command to look an account up."""
        return name or self._names.get(login, login)

    def can_write(self, login: str, key: Key) -> bool:
        """True for the operator and for `maintainers`. acli cannot say who else may be assigned the issue."""
        return login == self.operator() or login in self.maintainers

    # -- the card

    def statuses(self, keys: list[Key]) -> dict[Key, str]:
        wanted: list[Key] = list(dict.fromkeys(keys))
        found: dict[Key, str] = {}
        for start in range(0, len(wanted), KEYS_PER_CALL):
            quoted: str = ", ".join(f'"{key}"' for key in wanted[start:start + KEYS_PER_CALL])
            for row in self._search(f"key in ({quoted})"):
                found[row["key"]] = _status_name((row.get("fields") or {}).get("status"))
        return {key: status for key, status in found.items() if status}

    def last_move(self, key: Key, status: str) -> dict[str, Any] | None:
        """The card is not where the factory left it. acli shows no history, so nobody is named."""
        return {"by": None, "from": "", "to": status,
                "why": "This project reaches Jira through acli, and acli cannot tell who moved a card, "
                       "so a move is not an answer."}

    def _to_status(self, key: Key, status: str, setting: str) -> bool:
        current: str = _status_name((self._view(key, "status").get("fields") or {}).get("status"))
        if _same(current, status):
            return False
        res = self._acli("jira", "workitem", "transition", "--key", str(key), "--status", status, "--yes",
                         check=False)
        if res.returncode != 0:
            raise Refused(f"Jira did not move {key} from `{current}` to `{status}` ({setting}): {self._said(res)}. "
                          "Allow the transition in the Jira workflow, or map the step to another status.")
        return True

    # -- show and doctor

    def board_report(self) -> dict[str, Any]:
        """The mapping as factory.toml has it. Nothing is compared with Jira: acli has no board to read."""
        def row(step: str, kind: str, enabled: bool, status: str | None) -> dict[str, Any]:
            return {"step": step, "type": kind, "enabled": enabled, "status": status, "column": None,
                    "problem": None}

        rows: list[dict[str, Any]] = [row(board.QUEUE, "-", True, s) for s in board.queue_statuses(self.columns)]
        rows += [row(board.WATCH, "watch", True, s) for s in board.watch_statuses(self.columns)]
        rows += [row(name, step["type"], step.get("enabled", True), self.status_for(name))
                 for name, step in self.steps.items()]
        rows += [row(state, "end", True, self.status_for(state)) for state in ("done", "rejected", "failed")]
        return {"tracker": self.kind, "via": self.via, "site": self.site, "board": self.board, "rows": rows,
                "unused": [], "problems": [], "unchecked": [], "warnings": [],
                "note": f"not checked against Jira: {LIMITS}"}

    def doctor(self, ok, warn, fail) -> None:
        try:
            status = self._acli("jira", "auth", "status", check=False)
        except Refused as error:
            fail(str(error))
            return
        if status.returncode != 0:
            fail(f"acli is not signed in to Jira: run `acli jira auth login` ({self._said(status)})")
            return
        if not self.project:
            fail("[tracker.jira].project is missing: acli cannot read the board's filter, so the project's key "
                 "scopes the queue")
        try:
            who: str = self.mention(self.operator())
        except Refused as error:
            fail(str(error))
            return
        ok(f"Jira: signed in to {self.site} as {who}, through acli")
        warn(f"Jira through acli: a moved card is not an answer, and {LIMITS}")
        needy: list[str] = [name for name, step in self.steps.items()
                            if step.get("type") == "human" and step.get("enabled", True)
                            and config.who_for(step, "jira") == "maintainer"]
        if needy and not self.maintainers:
            warn("Jira through acli cannot tell who may be assigned an issue: set [tracker.jira].maintainers, "
                 f"or only the operator can answer {', '.join(needy)}")
