"""Pull requests on GitHub, through the `gh` CLI. Issues go through a tracker."""

import json
from typing import Any

from ..util import MARKER, Refused, run


def marked(comment: dict[str, Any]) -> dict[str, Any]:
    """A comment as `gh` returned it, with `factory` saying whether the factory wrote it.

    The factory's comments start with MARKER. It is taken off the body here, so nothing else looks for it.
    """
    text: str = comment.get("body") or ""
    return dict(comment, factory=text.startswith(MARKER), body=text.removeprefix(MARKER))


class GitHub:
    def __init__(self, repo):
        self.repo = repo

    def call(self, *args, input=None, check=True):
        try:
            return run(["gh", *map(str, args)], cwd=self.repo, input=input, check=check)
        except FileNotFoundError:
            raise Refused("the GitHub CLI `gh` isn't installed: https://cli.github.com") from None

    def json(self, *args):
        return json.loads(self.call(*args).stdout)

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
