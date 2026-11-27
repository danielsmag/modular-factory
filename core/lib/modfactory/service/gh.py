"""Pull requests on GitHub, through the `gh` CLI. Issues go through a tracker."""

import json
import subprocess
from typing import Any

from ..util import MARKER, Refused, run

# What the dashboard's GitHub block shows of each open issue.
ISSUE_FIELDS = "number,title,url,author,assignees,labels,createdAt,updatedAt"


def marked(comment: dict[str, Any]) -> dict[str, Any]:
    """A comment as `gh` returned it, with `factory` saying whether the factory wrote it.

    The factory's comments start with MARKER. It is taken off the body here, so nothing else looks for it.
    """
    text: str = comment.get("body") or ""
    return dict(comment, factory=text.startswith(MARKER), body=text.removeprefix(MARKER))


class GitHub:
    def __init__(self, repo):
        self.repo = repo

    def call(self, *args, input=None, check=True, timeout=None):
        try:
            return run(["gh", *map(str, args)], cwd=self.repo, input=input, check=check, timeout=timeout)
        except FileNotFoundError:
            raise Refused("the GitHub CLI `gh` isn't installed: https://cli.github.com") from None
        except subprocess.TimeoutExpired:
            said: str = " ".join(str(arg) for arg in args[:2])
            raise Refused(f"`gh {said}` did not answer within {timeout} s") from None

    def json(self, *args, timeout=None):
        return json.loads(self.call(*args, timeout=timeout).stdout)

    def open_issues(self, limit: int = 200, timeout: float = 30) -> list[dict[str, Any]]:
        """The repository's open issues, most recently updated first, at most `limit`."""
        found: object = self.json("issue", "list", "--state", "open", "--limit", limit, "--search",
                                  "sort:updated-desc", "--json", ISSUE_FIELDS, timeout=timeout)
        return [row for row in found if isinstance(row, dict)] if isinstance(found, list) else []

    def repo_url(self, timeout: float = 30) -> str | None:
        """The repository's address on GitHub."""
        found: object = self.json("repo", "view", "--json", "url", timeout=timeout)
        url: object = found.get("url") if isinstance(found, dict) else None
        return url if isinstance(url, str) else None

    def pr(self, ref, fields="number,url,state,isDraft,mergeCommit"):
        res = self.call("pr", "view", ref, "--json", fields, check=False)
        return json.loads(res.stdout) if res.returncode == 0 else None

    def open_prs(self, branch):
        return self.json("pr", "list", "--head", branch, "--state", "open", "--json", "number,url")

    def comments(self, number) -> list[dict[str, Any]]:
        """The pull request's comments, each saying whether the factory wrote it."""
        return [marked(c) for c in self.json("pr", "view", number, "--json", "comments")["comments"]]

    def comment(self, number, body) -> None:
        """A factory comment on the pull request."""
        self.call("pr", "comment", number, "--body-file", "-", input=f"{MARKER}\n{body}")
