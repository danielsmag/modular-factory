#!/usr/bin/env python3
"""A stand-in for the Atlassian CLI `acli`, for tests. It answers from the fake Jira of tests/stub_jira.py.

ACLI_STUB_SITE is that fake's address and ACLI_STUB_AUTH the Authorization header it accepts.
It implements only the commands the factory runs, with the flags Atlassian documents, and fails loudly on any
other. ACLI_STUB_LOG, when set, gets one line of JSON per call. ACLI_STUB_STRICT_FIELDS=1 makes `search`
refuse a field outside its documented default set. ACLI_STUB_LOGGED_OUT=1 answers as a CLI nobody signed in to.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SITE = os.environ.get("ACLI_STUB_SITE", "")
SEARCH_FIELDS = {"issuetype", "key", "assignee", "priority", "status", "summary"}


def call(method, path, body=None, **params):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    request = urllib.request.Request(
        f"{SITE}{urllib.parse.quote(path)}" + (f"?{query}" if query else ""), method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": os.environ["ACLI_STUB_AUTH"], "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as res:
            raw = res.read()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as error:
        said = json.loads(error.read() or b"{}").get("errorMessages") or [f"HTTP {error.code}"]
        sys.exit(f"✗ Error: {'; '.join(said)}")


def pages(path, key, **params):
    out = []
    while True:
        found = call("GET", path, startAt=len(out), maxResults=100, **params)
        out += found[key]
        if not found[key] or len(out) >= found["total"]:
            return out


def flags(args):
    """Split argv into positionals and --flag values. A flag followed by another flag, or by nothing, is bare."""
    positional, named, i = [], {}, 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            has_value = i + 1 < len(args) and not args[i + 1].startswith("--") and arg not in BARE
            named[arg.lstrip("-")] = args[i + 1] if has_value else True
            i += 2 if has_value else 1
        else:
            positional.append(arg)
            i += 1
    return positional, named


BARE = {"--json", "--paginate", "--yes", "--csv", "--count", "--web", "--ignore-errors"}


def document(text):
    """What a body file holds: a document when it is ADF, else plain text as one paragraph per block."""
    try:
        found = json.loads(text)
        if isinstance(found, dict) and found.get("type") == "doc":
            return found
    except ValueError:
        pass
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": block}]} for block in text.split("\n\n") if block]}


def main(argv):
    if os.environ.get("ACLI_STUB_LOG"):
        with open(os.environ["ACLI_STUB_LOG"], "a") as log:
            log.write(json.dumps(argv) + "\n")
    if argv[:1] != ["jira"]:
        sys.exit(f"stub acli: unknown command {' '.join(argv)}")
    if os.environ.get("ACLI_STUB_LOGGED_OUT"):
        sys.exit("✗ Error: unauthorized: use 'acli jira auth login' to authenticate")
    group, rest = argv[1:3], argv[3:]
    if group == ["auth", "status"]:
        print(f"✓ Authenticated\n  Site: {SITE}\n  Email: operator@example.test\n  Authentication Type: api_token")
        return
    if group[0] != "workitem":
        sys.exit(f"stub acli: unknown command {' '.join(argv)}")
    action = group[1]
    if action == "comment":
        action, rest = f"comment {rest[0]}", rest[1:]
    pos, named = flags(rest)

    if action == "search":
        wanted = named["fields"].split(",")
        if os.environ.get("ACLI_STUB_STRICT_FIELDS") and set(wanted) - SEARCH_FIELDS:
            sys.exit(f"✗ Error: field '{sorted(set(wanted) - SEARCH_FIELDS)[0]}' is not allowed")
        if not named.get("json"):
            sys.exit("stub acli: search without --json")
        rows = pages("/rest/api/3/search/jql", "issues", jql=named["jql"])
        if "limit" in named:
            rows = rows[:int(named["limit"])]
        print(json.dumps([{"key": row["key"], "fields": {f: row["fields"].get(f) for f in wanted if f != "key"}}
                          for row in rows]))
    elif action == "view":
        wanted = named["fields"].split(",")
        found = call("GET", f"/rest/api/3/issue/{pos[0]}")
        fields = {f: found["fields"].get(f) for f in wanted if f != "comment"}
        if "comment" in wanted:
            page = call("GET", f"/rest/api/3/issue/{pos[0]}/comment")
            fields["comment"] = {k: page[k] for k in ("comments", "startAt", "maxResults", "total")}
        print(json.dumps({"key": found["key"], "id": found["id"], "fields": fields}))
    elif action == "comment list":
        print(json.dumps({"comments": pages(f"/rest/api/3/issue/{named['key']}/comment", "comments")}))
    elif action == "comment create":
        with open(named["body-file"]) as handle:
            call("POST", f"/rest/api/3/issue/{named['key']}/comment", {"body": document(handle.read())})
        print("✓ Comment created")
    elif action == "transition":
        if not named.get("yes"):
            sys.exit("stub acli: transition would prompt without --yes")
        found = call("GET", f"/rest/api/3/issue/{named['key']}", expand="transitions")
        chosen = next((t for t in found["transitions"] if t["to"]["name"] == named["status"]), None)
        if chosen is None:
            sys.exit(f"✗ Error: {named['key']}: no transition to status '{named['status']}' is available")
        call("POST", f"/rest/api/3/issue/{named['key']}/transitions", {"transition": {"id": chosen["id"]}})
        print(f"✓ {named['key']} transitioned to {named['status']}")
    elif action == "create":
        with open(named["description-file"]) as handle:
            description = document(handle.read())
        created = call("POST", "/rest/api/3/issue", {"fields": {
            "project": {"key": named["project"]}, "summary": named["summary"], "description": description,
            "issuetype": {"name": named["type"]}}})
        print(json.dumps(created))
    else:
        sys.exit(f"stub acli: unknown command {' '.join(argv)}")


if __name__ == "__main__":
    main(sys.argv[1:])
