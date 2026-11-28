"""GitHub issues as the tracker: the queue is a label, and people reply in issue comments."""

import json
import re
from subprocess import CompletedProcess
from typing import Any

from ...util import MARKER, Refused
from ..gh import GitHub, marked
from .base import RELATION_TEXT, Key, OneTracker, Who

WRITERS = {"admin", "maintain", "write"}
# The labels `install` creates: colour and description for each `[labels]` key.
LABELS = {
    "queue": ("ff4a26", "The software factory builds this issue"),
    "needs_info": ("d4a72c", "The factory is waiting for the issue's author"),
    "needs_human": ("b60205", "The factory is waiting for a maintainer"),
}


def _failure(res: CompletedProcess[str]) -> str:
    why: list[str] = (res.stderr or res.stdout).strip().splitlines()
    return why[0] if why else "gh failed"


class GitHubTracker(OneTracker):
    kind = "github"
    label = "GitHub"
    # A person may be asked, and may reply, on the job's pull request as well as on the issue.
    pr_threads = True
    comment_limit = 60000
    # The pull request links the issue from its body, so its title is free.
    links_by_title = False
    # GitHub issues have no board.
    drag_answers = False

    def __init__(self, gh: GitHub, labels: dict[str, str], issue_link: str = "refs"):
        self.gh = gh
        self.labels = labels
        self.issue_link = issue_link
        self._operator: str | None = None
        self._writers: dict[str, bool] = {}

    @classmethod
    def from_config(cls, cfg, gh: GitHub) -> "GitHubTracker":
        return cls(gh, cfg["labels"], cfg["project"].get("issue_link", "refs"))

    # -- keys

    def key(self, ref: object) -> Key:
        text: str = str(ref).strip().lstrip("#")
        if not text.isdigit():
            raise Refused(f"{str(ref)!r} is not an issue number")
        return int(text)

    def ref(self, key: Key) -> str:
        return f"#{key}"

    # -- issues and the queue

    def issue(self, key: Key) -> dict[str, Any]:
        found: dict[str, Any] = self.gh.json("issue", "view", key, "--json",
                                             "number,title,body,author,labels,state,url,comments")
        found["comments"] = [marked(c) for c in found["comments"]]
        return found

    def issue_details(self, key: Key) -> object:
        """The issue for the dashboard's drawer. A failure names only gh's last line, not the command."""
        res: CompletedProcess[str] = self.gh.call(
            "issue", "view", key, "--json",
            "number,title,body,author,labels,state,url,createdAt,updatedAt,comments", check=False)
        if res.returncode != 0:
            lines: list[str] = (res.stderr or res.stdout).strip().splitlines()
            raise Refused(lines[-1] if lines else f"gh exited with code {res.returncode}")
        return json.loads(res.stdout)

    def queue(self) -> list[dict[str, Any]]:
        """The open issues with the queue label, oldest number first."""
        found: list[dict[str, Any]] = self.gh.json(
            "issue", "list", "--label", self.labels["queue"], "--state", "open", "--limit", "200",
            "--json", "number,title,url,createdAt,updatedAt,author,assignees,labels")
        return sorted(found, key=lambda issue: issue["number"])

    def watched(self) -> list[dict[str, Any]]:
        """GitHub has no watched issues: an issue is in the queue or not."""
        return []

    def withdrawn(self, key: Key, queued: set[Key]) -> str | None:
        if key in queued:
            return None
        return f"issue is closed or no longer labeled {self.labels['queue']}"

    def queue_note(self) -> str:
        return f"the queue is the `{self.labels['queue']}` label"

    def empty_note(self) -> str:
        return f"no open issue is labeled `{self.labels['queue']}`"

    def enqueue_hint(self) -> str:
        return f"Label an issue `{self.labels['queue']}`"

    def requeue_hint(self) -> str:
        return "put the factory label back to run it again"

    def queue_gate(self, issue: dict[str, Any]) -> str | None:
        label: str = self.labels["queue"]
        if label in {found["name"] for found in issue["labels"]}:
            return None
        return f"issue {self.ref(issue['number'])} isn't labeled `{label}`. Only labeled issues run."

    def dequeue(self, key: Key) -> None:
        self._label(key, remove=self.labels["queue"])

    def create_issue(self, title: str, body: str, queue: bool = False) -> str:
        label_args: list[str] = ["--label", self.labels["queue"]] if queue else []
        return self.gh.call("issue", "create", "--title", title, *label_args, "--body-file", "-",
                            input=body).stdout.strip()

    def close_issue(self, key: Key, reason: str = "completed") -> bool:
        """Close the issue as `completed` or `not planned`. False when it was already closed."""
        found: object = self.gh.json("issue", "view", key, "--json", "state")
        if isinstance(found, dict) and found.get("state") != "OPEN":
            return False
        self.gh.call("issue", "close", key, "--reason", reason)
        return True

    # -- comments

    def comments(self, key: Key) -> list[dict[str, Any]]:
        return [marked(c) for c in self.gh.json("issue", "view", key, "--json", "comments")["comments"]]

    def comment(self, key: Key, body: str, factory: bool = True) -> None:
        self.gh.call("issue", "comment", key, "--body-file", "-", input=f"{MARKER}\n{body}" if factory else body)

    def link(self, key: Key, other: str, relation: str) -> None:
        """GitHub has no issue links in the API the factory uses, so the relation is said in a comment."""
        self.comment(key, f"{RELATION_TEXT[relation]} {other}")

    # -- people

    def operator(self) -> str:
        """The GitHub user running the factory. Its factory comments carry MARKER."""
        if self._operator is None:
            self._operator = self.gh.json("api", "user")["login"]
        return self._operator

    def mention(self, login: str, name: str | None = None) -> str:
        return f"@{login}"

    def can_write(self, login: str, key: Key | None = None) -> bool:
        """True for the operator and for anyone with write access to the repo."""
        if login == self.operator():
            return True
        if login not in self._writers:
            res = self.gh.call("api", f"repos/{{owner}}/{{repo}}/collaborators/{login}/permission", check=False)
            level = json.loads(res.stdout).get("permission") if res.returncode == 0 else None
            self._writers[login] = level in WRITERS
        return self._writers[login]

    # -- waiting for a person

    def waiting_on(self, key: Key, who: Who) -> str | None:
        label: str = self.labels["needs_info"] if who == "author" else self.labels["needs_human"]
        self._label(key, add=label)
        return label

    def waiting_off(self, key: Key, flag: str | None) -> None:
        self._label(key, remove=flag)

    def _label(self, key: Key, add: str | None = None, remove: str | None = None) -> None:
        """Labels are bookkeeping, so a label that is already gone is not an error."""
        if add:
            self.gh.call("issue", "edit", key, "--add-label", add)
        if remove:
            self.gh.call("issue", "edit", key, "--remove-label", remove, check=False)

    # -- the board: GitHub issues have none

    def move(self, key: Key, step: str) -> None:
        return None

    def status_for(self, step: str) -> str | None:
        return None

    def statuses(self, keys: list[Key]) -> dict[Key, str]:
        return {}

    def last_move(self, key: Key, status: str) -> dict[str, Any] | None:
        return None

    def board_report(self) -> dict[str, Any] | None:
        return None

    # -- the pull request's link to the issue

    def branch_tag(self, key: Key) -> str:
        return ""

    def pr_title_problem(self, title: str, key: Key) -> str | None:
        return None

    def pr_link_rule(self) -> str:
        if self.issue_link == "closes":
            return "The first line is `Closes #<issue>`. GitHub closes the issue when the pull request merges."
        return ("The first line is `Refs #<issue>`. Do not put a closing keyword (close, fix, resolve) "
                "next to the issue number: the issue stays open after the merge.")

    def pr_link_problem(self, body: str, key: Key) -> str | None:
        """With `issue_link = "refs"`, a body that would not link the issue, or would close it on merge."""
        if self.issue_link == "closes":
            return None
        lines: list[str] = body.strip().splitlines()
        if not lines or lines[0].strip() != f"Refs #{key}":
            return f"body must start with `Refs #{key}` ([project].issue_link = \"refs\")."
        if re.search(rf"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#{key}\b", body):
            return (f"body has a closing keyword next to #{key}, so GitHub would close the issue on merge "
                    "([project].issue_link = \"refs\").")
        return None

    # -- install, doctor and remove

    def install(self, say) -> None:
        """Create the factory's labels that are missing. Existing labels are left as they are."""
        later = "      Create the labels later with `.factory/bin/mfactory install`."
        try:
            res = self.gh.call("label", "list", "--limit", "300", "--json", "name", check=False)
        except Refused:
            say("warn  the GitHub CLI `gh` isn't installed, so the labels were not created")
            return
        if res.returncode != 0:
            say(f"warn  the labels could not be listed: {_failure(res)}")
            say(later)
            return
        have = {label["name"] for label in json.loads(res.stdout or "[]")}
        created: list[str] = []
        for key, (colour, description) in LABELS.items():
            name = self.labels[key]
            if name in have:
                say(f"ok    label {name} already exists")
                continue
            res = self.gh.call("label", "create", name, "--color", colour, "--description", description,
                               check=False)
            if res.returncode != 0:
                say(f"warn  label `{name}` was not created: {_failure(res)}")
                say(later)
                return
            created.append(name)
        if created:
            say("ok    GitHub labels created: " + ", ".join(created))

    def doctor(self, ok, warn, fail) -> None:
        try:
            res = self.gh.call("label", "list", "--limit", "300", "--json", "name", check=False)
        except Refused:
            return  # doctor has already said `gh` is missing
        if res.returncode != 0:
            warn("could not list the repository's labels")
            return
        have = {label["name"] for label in json.loads(res.stdout)}
        gone = [n for n in self.labels.values() if n not in have]
        (fail if gone else ok)(f"GitHub labels missing: {', '.join(gone)}. Run `mfactory install`." if gone
                               else "GitHub labels exist")

    def uninstall(self, say) -> None:
        for name in self.labels.values():
            res = self.gh.call("label", "delete", name, "--yes", check=False)
            if res.returncode == 0:
                say(f"ok    label {name} deleted")
            else:
                say(f"warn  label `{name}` was not deleted: {_failure(res)}")
