"""Everything the factory asks of GitHub, through the `gh` CLI."""

import json
from subprocess import CompletedProcess

from ..util import MARKER, Refused, run

WRITERS = {"admin", "maintain", "write"}


class GitHub:
    def __init__(self, repo):
        self.repo = repo
        self._operator = None
        self._writers = {}

    def call(self, *args, input=None, check=True):
        try:
            return run(["gh", *map(str, args)], cwd=self.repo, input=input, check=check)
        except FileNotFoundError:
            raise Refused("the GitHub CLI `gh` isn't installed: https://cli.github.com") from None

    def json(self, *args):
        return json.loads(self.call(*args).stdout)

    def operator(self):
        """The GitHub user running the factory. Its factory comments carry MARKER."""
        if self._operator is None:
            self._operator = self.json("api", "user")["login"]
        return self._operator

    def can_write(self, login):
        """True for the operator and for anyone with write access to the repo."""
        if login == self.operator():
            return True
        if login not in self._writers:
            res = self.call("api", f"repos/{{owner}}/{{repo}}/collaborators/{login}/permission", check=False)
            level = json.loads(res.stdout).get("permission") if res.returncode == 0 else None
            self._writers[login] = level in WRITERS
        return self._writers[login]

    # -- issues

    def issue(self, number):
        return self.json("issue", "view", number, "--json",
                         "number,title,body,author,labels,state,url,comments")

    def issue_details(self, number: int) -> object:
        """The issue for the dashboard's drawer. A failure names only gh's last line, not the command."""
        res: CompletedProcess[str] = self.call(
            "issue", "view", number, "--json",
            "number,title,body,author,labels,state,url,createdAt,updatedAt,comments", check=False)
        if res.returncode != 0:
            lines: list[str] = (res.stderr or res.stdout).strip().splitlines()
            raise Refused(lines[-1] if lines else f"gh exited with code {res.returncode}")
        return json.loads(res.stdout)

    def queue(self, label):
        return self.json("issue", "list", "--label", label, "--state", "open", "--limit", "200",
                         "--json", "number,title,url,createdAt,updatedAt,author,assignees,labels")

    def create_issue(self, title, body, label):
        return self.call("issue", "create", "--title", title, "--label", label, "--body-file", "-",
                         input=body).stdout.strip()

    def close_issue(self, number: int, reason: str = "completed") -> bool:
        """Close the issue as `completed` or `not planned`. False when it was already closed."""
        found: object = self.json("issue", "view", number, "--json", "state")
        if isinstance(found, dict) and found.get("state") != "OPEN":
            return False
        self.call("issue", "close", number, "--reason", reason)
        return True

    def comments(self, kind, number):
        return self.json(kind, "view", number, "--json", "comments")["comments"]

    def comment(self, kind, number, body, marker=True):
        self.call(kind, "comment", number, "--body-file", "-", input=f"{MARKER}\n{body}" if marker else body)

    def label(self, number, add=None, remove=None):
        """Labels are bookkeeping, so a label that is already gone is not an error."""
        if add:
            self.call("issue", "edit", number, "--add-label", add)
        if remove:
            self.call("issue", "edit", number, "--remove-label", remove, check=False)

    # -- pull requests

    def pr(self, ref, fields="number,url,state,isDraft,mergeCommit"):
        res = self.call("pr", "view", ref, "--json", fields, check=False)
        return json.loads(res.stdout) if res.returncode == 0 else None

    def open_prs(self, branch):
        return self.json("pr", "list", "--head", branch, "--state", "open", "--json", "number,url")
