#!/usr/bin/env python3
"""A stand-in for the GitHub CLI, for tests. It keeps issues and pull requests in a JSON file.

GH_STATE names the file and GH_ORIGIN the bare repository that plays GitHub.
It implements only the calls the factory makes, and fails loudly on any other.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

STATE = Path(os.environ["GH_STATE"])
ORIGIN = os.environ.get("GH_ORIGIN", "")


def load():
    return json.loads(STATE.read_text())


def save(state):
    STATE.write_text(json.dumps(state, indent=1))


def flags(args):
    """Split argv into positionals and --flag values."""
    positional, named, i = [], {}, 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            takes_value = i + 1 < len(args) and not args[i + 1].startswith("--") and arg not in BARE
            named[arg[2:]] = args[i + 1] if takes_value else True
            i += 2 if takes_value else 1
        else:
            positional.append(arg)
            i += 1
    return positional, named


BARE = {"--draft", "--squash", "--merge", "--rebase", "--force"}


def pick(record, fields):
    return {f: record[f] for f in fields.split(",")}


def body_of(named):
    if named.get("body-file") == "-":
        return sys.stdin.read()
    return named.get("body", "")


def comment(state, record, text):
    state["next_comment"] += 1
    record["comments"].append({
        "id": f"C{state['next_comment']}", "author": {"login": os.environ.get("GH_USER", state["user"])},
        "body": text, "createdAt": "2026-01-01T00:00:00Z"})


def find_pr(state, ref):
    for pr in state["prs"].values():
        if str(pr["number"]) == str(ref) or pr["headRefName"] == ref:
            return pr
    return None


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def merge(pr, method):
    """Merge the pull request's branch into its base in the origin repository."""
    with tempfile.TemporaryDirectory() as tmp:
        git("clone", "-q", ORIGIN, tmp, cwd=".")
        git("config", "user.name", "gh", cwd=tmp)
        git("config", "user.email", "gh@example.com", cwd=tmp)
        git("checkout", "-q", pr["baseRefName"], cwd=tmp)
        if method == "squash":
            git("merge", "--squash", f"origin/{pr['headRefName']}", cwd=tmp)
            git("commit", "-q", "-m", f"{pr['title']} (#{pr['number']})", cwd=tmp)
        else:
            git("merge", "--no-ff", "-m", f"Merge #{pr['number']}", f"origin/{pr['headRefName']}", cwd=tmp)
        git("push", "-q", "origin", pr["baseRefName"], cwd=tmp)
        return git("rev-parse", "HEAD", cwd=tmp)


def main(argv):
    state = load()
    state["calls"].append(argv)
    group, action, rest = argv[0], argv[1] if len(argv) > 1 else "", argv[2:]
    pos, named = flags(rest)
    out = ""

    if state.get("fail") and " ".join(argv).startswith(state["fail"]):
        save(state)
        sys.exit(f"stub gh: told to fail on `{state['fail']}`")

    if group in ("auth", "--version"):
        out = "gh version 0.0.0 (stub)" if group == "--version" else ""
    elif group == "api":
        if action == "user":
            out = json.dumps({"login": state["user"]})
        elif "/collaborators/" in action:
            login = action.split("/collaborators/")[1].split("/")[0]
            out = json.dumps({"permission": state["permissions"].get(login, "read")})
        else:
            sys.exit(f"stub gh: unknown api call {action}")
    elif group == "label":
        if action == "create":
            state["labels"] = sorted(set(state["labels"]) | {pos[0]})
        elif action == "list":
            out = json.dumps([{"name": n} for n in state["labels"]])
        elif action == "delete":
            state["labels"] = [n for n in state["labels"] if n != pos[0]]
    elif group == "issue":
        if action == "view":
            issue = state["issues"].get(pos[0])
            if not issue:
                sys.exit(f"stub gh: no issue {pos[0]}")
            out = json.dumps(pick(issue, named["json"]))
        elif action == "list":
            rows = [i for i in state["issues"].values()
                    if i["state"] == "OPEN" and named["label"] in [label["name"] for label in i["labels"]]]
            out = json.dumps([pick(i, named["json"]) for i in rows])
        elif action == "create":
            state["next_number"] += 1
            n = state["next_number"]
            state["issues"][str(n)] = {
                "number": n, "title": named["title"], "body": body_of(named), "state": "OPEN",
                "author": {"login": state["user"]}, "labels": [{"name": named["label"]}],
                "url": f"https://github.test/acme/app/issues/{n}", "comments": []}
            out = state["issues"][str(n)]["url"]
        elif action == "comment":
            comment(state, state["issues"][pos[0]], body_of(named))
        elif action == "edit":
            issue = state["issues"][pos[0]]
            names = [label["name"] for label in issue["labels"]]
            if "add-label" in named and named["add-label"] not in names:
                names.append(named["add-label"])
            if "remove-label" in named:
                if named["remove-label"] not in names:
                    save(state)
                    sys.exit("stub gh: label not on issue")
                names.remove(named["remove-label"])
            issue["labels"] = [{"name": n} for n in names]
        else:
            sys.exit(f"stub gh: unknown issue action {action}")
    elif group == "pr":
        if action == "create":
            state["next_number"] += 1
            n = state["next_number"]
            state["prs"][str(n)] = {
                "number": n, "title": named["title"], "body": body_of(named), "state": "OPEN",
                "isDraft": bool(named.get("draft")), "headRefName": named["head"], "baseRefName": named["base"],
                "url": f"https://github.test/acme/app/pull/{n}", "mergeCommit": None, "comments": []}
            out = state["prs"][str(n)]["url"]
        elif action == "list":
            rows = [p for p in state["prs"].values() if p["headRefName"] == named["head"] and p["state"] == "OPEN"]
            out = json.dumps([pick(p, named["json"]) for p in rows])
        else:
            pr = find_pr(state, pos[0])
            if not pr:
                save(state)
                sys.exit(f"stub gh: no pull request {pos[0]}")
            if action == "view":
                out = json.dumps(pick(pr, named["json"]))
            elif action == "comment":
                comment(state, pr, body_of(named))
            elif action == "ready":
                pr["isDraft"] = False
            elif action == "close":
                pr["state"] = "CLOSED"
            elif action == "merge":
                if pr["state"] != "OPEN" or pr["isDraft"]:
                    save(state)
                    sys.exit("stub gh: pull request is not open and ready")
                method = next(m for m in ("squash", "merge", "rebase") if m in named)
                pr["mergeCommit"] = {"oid": merge(pr, method)}
                pr["state"] = "MERGED"
            else:
                sys.exit(f"stub gh: unknown pr action {action}")
    else:
        sys.exit(f"stub gh: unknown command {' '.join(argv)}")

    save(state)
    if out:
        print(out)


if __name__ == "__main__":
    main(sys.argv[1:])
