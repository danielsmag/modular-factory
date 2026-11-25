"""A stand-in for Jira Cloud, for tests: a local HTTP server that keeps a board and its issues in memory.

It implements only the REST calls the factory makes, with the shapes of Atlassian's published API, and
answers 501 to any other, so a test notices a call nobody planned for.
"""

import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

EMAIL = "operator@example.test"
TOKEN = "jira-token-for-tests"
# Column name and the statuses mapped to it, left to right. "Archived" is a status on no column.
COLUMNS = [(name, [name]) for name in (
    "Backlog", "Ready for factory", "Intake", "Needs info", "Spec", "Spec approval", "Build", "Checks",
    "Debug", "Review", "Approve", "Ready to merge", "Merging", "Ready to verify", "Verify", "Escalated",
    "Done", "Rejected", "Failed")]
DONE_CATEGORY = {"Done", "Rejected", "Failed", "Archived"}


def document(text):
    """Plain text as a Jira document: one paragraph per blank-line block, a hard break per line end."""
    blocks = []
    for chunk in text.split("\n\n"):
        content = []
        for number, line in enumerate(chunk.split("\n")):
            if number:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        blocks.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": blocks}


def plain(node):
    """A document's text, enough to assert on what the factory wrote."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        coded = any(mark.get("type") == "code" for mark in node.get("marks", []))
        return f"`{node['text']}`" if coded else node["text"]
    if node.get("type") == "hardBreak":
        return "\n"
    inner = [plain(child) for child in node.get("content", [])]
    blocks = node.get("type") in ("doc", "bulletList", "orderedList")
    return ("\n" if blocks else "").join(inner)


class Problem(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class FakeJira:
    def __init__(self):
        self.lock = threading.RLock()
        self.users = {
            "op": "Olivia Operator", "alice": "Alice Author", "maint": "Mia Maintainer", "bob": "Bob Bystander"}
        self.me = "op"
        self.assignable = {"op", "maint"}
        self.project = "PROJ"
        self.board_id = 7
        self.columns = [(name, list(statuses)) for name, statuses in COLUMNS]
        names = [status for _, statuses in COLUMNS for status in statuses] + ["Archived"]
        self.statuses = {name: str(10000 + index) for index, name in enumerate(names)}
        self.workflow = None          # None: any status to any other. Else {from: [to, ...]}
        self.issues = {}
        self.calls = []               # (method, path) of every request
        self.page_size = 50
        self.fail = None              # (path prefix, status): answer that status once the path starts so
        self.next_issue = 0
        self.next_id = 1000
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                fake.handle(self, "GET")

            def do_POST(self):
                fake.handle(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ---- What a test does to the board ----------------------------------------

    def _id(self):
        self.next_id += 1
        return str(self.next_id)

    def ticket(self, title="Add a thing", body="Please add it.", status="Ready for factory", reporter="alice",
               assignee=None, labels=()):
        with self.lock:
            self.next_issue += 1
            key = f"{self.project}-{self.next_issue}"
            self.issues[key] = {
                "id": self._id(), "key": key, "summary": title, "description": document(body), "status": status,
                "reporter": reporter, "assignee": assignee, "labels": list(labels), "comments": [],
                "changelog": [], "created": "2026-01-01T00:00:00.000+0000", "updated": "2026-01-02T00:00:00.000+0000"}
            return key

    def comment(self, key, text, user, properties=()):
        with self.lock:
            found = {"id": self._id(), "author": user, "body": document(text),
                     "created": "2026-01-03T00:00:00.000+0000", "properties": list(properties)}
            self.issues[key]["comments"].append(found)
            return found["id"]

    def drag(self, key, status, user):
        """A person moves the card on the board."""
        with self.lock:
            self._transition(self.issues[key], status, user)

    def status(self, key):
        return self.issues[key]["status"]

    def comments(self, key):
        return [plain(c["body"]) for c in self.issues[key]["comments"]]

    def moves(self, key):
        """Every status change of the card, as (who, from, to)."""
        return [(entry["author"], item["fromString"], item["toString"])
                for entry in self.issues[key]["changelog"] for item in entry["items"]]

    def called(self, method, fragment):
        return [path for verb, path in self.calls if verb == method and fragment in path]

    # ---- The REST API ------------------------------------------------------------

    def _transition(self, issue, status, user):
        if status not in self.statuses:
            raise Problem(400, f"no status {status}")
        entry = {"id": self._id(), "author": user, "created": "2026-01-04T00:00:00.000+0000", "items": [{
            "field": "status", "fieldId": "status", "fieldtype": "jira", "from": self.statuses[issue["status"]],
            "fromString": issue["status"], "to": self.statuses[status], "toString": status}]}
        issue["changelog"].append(entry)
        issue["status"] = status

    def _user(self, account):
        return {"accountId": account, "displayName": self.users[account], "active": True,
                "accountType": "atlassian"}

    def _status(self, name):
        category = "done" if name in DONE_CATEGORY else "indeterminate"
        return {"id": self.statuses[name], "name": name, "statusCategory": {"key": category}}

    def _comment(self, comment, properties=False):
        out = {"id": comment["id"], "author": self._user(comment["author"]), "body": comment["body"],
               "created": comment["created"], "updated": comment["created"]}
        if properties:
            out["properties"] = comment["properties"]
        return out

    def _issue(self, issue):
        assignee = issue["assignee"]
        return {"id": issue["id"], "key": issue["key"], "self": f"{self.url}/rest/api/3/issue/{issue['id']}",
                "fields": {
                    "summary": issue["summary"], "description": issue["description"],
                    "status": self._status(issue["status"]), "reporter": self._user(issue["reporter"]),
                    "assignee": self._user(assignee) if assignee else None, "labels": issue["labels"],
                    "created": issue["created"], "updated": issue["updated"]}}

    def _allowed(self, issue):
        """The statuses the workflow lets this issue move to."""
        if self.workflow is None:
            return [name for name in self.statuses if name != issue["status"]]
        return list(self.workflow.get(issue["status"], []))

    def _transitions(self, issue):
        return [{"id": self.statuses[name], "name": f"To {name}", "to": self._status(name)}
                for name in self._allowed(issue)]

    def _matches(self, issue, jql):
        """The small part of JQL the factory writes, and `assignee = currentUser()` for a project's own filter."""
        jql = re.split(r"(?i)\s+order\s+by\s+", jql.strip())[0].strip()
        if not jql:
            return True
        parts, depth, start = [], 0, 0
        for index, char in enumerate(jql):
            depth += (char == "(") - (char == ")")
            if depth == 0 and jql[index:index + 5].upper() == " AND ":
                parts.append(jql[start:index])
                start = index + 5
        parts.append(jql[start:])
        if len(parts) > 1:
            return all(self._matches(issue, part) for part in parts)
        clause = parts[0].strip()
        if clause.startswith("(") and clause.endswith(")"):
            return self._matches(issue, clause[1:-1])
        if found := re.fullmatch(r"(?i)status in \((.*)\)", clause):
            return issue["status"] in re.findall(r'"([^"]*)"', found.group(1))
        if found := re.fullmatch(r"(?i)(?:issue)?key in \((.*)\)", clause):
            return issue["key"] in re.findall(r'"([^"]*)"', found.group(1))
        if found := re.fullmatch(r'(?i)(?:issue)?key = "?([A-Za-z0-9_]+-\d+)"?', clause):
            return issue["key"] == found.group(1).upper()
        if re.fullmatch(r"(?i)assignee = currentUser\(\)", clause):
            return issue["assignee"] == self.me
        if re.fullmatch(r"(?i)reporter = currentUser\(\)", clause):
            return issue["reporter"] == self.me
        if found := re.fullmatch(r'(?i)project = "?([A-Za-z0-9_]+)"?', clause):
            return issue["key"].rsplit("-", 1)[0] == found.group(1).upper()
        if found := re.fullmatch(r"(?i)labels = (\w+)", clause):
            return found.group(1) in issue["labels"]
        raise Problem(400, f"stub jira: JQL it cannot read: {clause}")

    def _page(self, rows, query, key, extra=None):
        start = int(query.get("startAt", ["0"])[0])
        size = min(int(query.get("maxResults", [str(self.page_size)])[0]), self.page_size)
        page = rows[start:start + size]
        return {key: page, "startAt": start, "maxResults": size, "total": len(rows),
                "isLast": start + len(page) >= len(rows), **(extra or {})}

    def handle(self, request, method):
        parts = urlsplit(request.path)
        path, query = unquote(parts.path), parse_qs(parts.query)
        length = int(request.headers.get("Content-Length") or 0)
        body = json.loads(request.rfile.read(length) or b"{}") if length else {}
        with self.lock:
            self.calls.append((method, path))
            try:
                expected = "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
                if request.headers.get("Authorization") != expected:
                    raise Problem(401, "Client must be authenticated to access this resource.")
                if self.fail and path.startswith(self.fail[0]):
                    raise Problem(self.fail[1], "stub jira: told to fail")
                status, payload = self.route(method, path, query, body)
            except Problem as problem:
                status, payload = problem.status, {"errorMessages": [str(problem)], "errors": {}}
        data = b"" if payload is None else json.dumps(payload).encode()
        request.send_response(status)
        request.send_header("Content-Type", "application/json")
        request.send_header("Content-Length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)

    def _find(self, key):
        issue = self.issues.get(key.upper())
        if not issue:
            raise Problem(404, "Issue does not exist or you do not have permission to see it.")
        return issue

    def route(self, method, path, query, body):
        if method == "GET" and path == "/rest/api/3/myself":
            return 200, self._user(self.me)
        if method == "GET" and path == "/rest/api/3/user":
            account = query.get("accountId", [""])[0]
            if account not in self.users:
                raise Problem(404, "user not found")
            return 200, self._user(account)
        if method == "GET" and path == "/rest/api/3/status":
            return 200, [self._status(name) for name in self.statuses]
        if method == "GET" and path == "/rest/api/3/user/assignable/search":
            self._find(query["issueKey"][0])
            account = query.get("accountId", [""])[0]
            return 200, [self._user(account)] if account in self.assignable else []
        if method == "GET" and path == "/rest/api/3/search/jql":
            # Every issue the account can see, on the board or not. The factory's REST client never asks
            # this; the stub `acli` does, as the real one searches Jira and not a board.
            jql = query.get("jql", [""])[0]
            ordered = sorted(self.issues.values(), key=lambda issue: int(issue["id"]))
            return 200, self._page([self._issue(i) for i in ordered if self._matches(i, jql)], query, "issues")
        if method == "POST" and path == "/rest/api/3/issue":
            fields = body["fields"]
            if fields["project"]["key"] != self.project:
                raise Problem(400, "project is required")
            key = self.ticket(fields["summary"], status="Backlog", reporter=self.me)
            self.issues[key]["description"] = fields.get("description")
            self.issues[key]["type"] = fields["issuetype"]["name"]
            return 201, {"id": self.issues[key]["id"], "key": key, "self": f"{self.url}/rest/api/3/issue/{key}"}

        if found := re.fullmatch(r"/rest/api/3/issue/([^/]+)(?:/(comment|transitions|changelog))?", path):
            issue, what = self._find(found.group(1)), found.group(2)
            if what is None and method == "GET":
                found = self._issue(issue)
                if "transitions" in query.get("expand", [""])[0]:
                    found["transitions"] = self._transitions(issue)
                return 200, found
            if what == "comment" and method == "GET":
                return 200, self._page([self._comment(c) for c in issue["comments"]], query, "comments")
            if what == "comment" and method == "POST":
                ident = self.comment(issue["key"], "", self.me, body.get("properties", []))
                issue["comments"][-1]["body"] = body["body"]
                return 201, self._comment(issue["comments"][-1]) | {"id": ident}
            if what == "transitions" and method == "GET":
                return 200, {"transitions": self._transitions(issue)}
            if what == "transitions" and method == "POST":
                wanted = body["transition"]["id"]
                target = next((name for name in self._allowed(issue) if self.statuses[name] == wanted), None)
                if target is None:
                    raise Problem(400, "Transition is not valid for this issue.")
                self._transition(issue, target, self.me)
                return 204, None
            if what == "changelog" and method == "GET":
                rows = [dict(entry, author=self._user(entry["author"])) for entry in issue["changelog"]]
                return 200, self._page(rows, query, "values")

        if found := re.fullmatch(r"/rest/agile/1\.0/board/(\d+)(?:/(issue|configuration))?", path):
            if int(found.group(1)) != self.board_id or method != "GET":
                raise Problem(404, "The requested board cannot be viewed because it either does not exist "
                                   "or you do not have permission to view it.")
            what = found.group(2)
            if what is None:
                return 200, {"id": self.board_id, "name": "Factory board", "type": "kanban",
                             "location": {"projectKey": self.project, "projectName": "Project"}}
            if what == "configuration":
                return 200, {"id": self.board_id, "name": "Factory board", "type": "kanban",
                             "location": {"type": "project", "projectKeyOrId": self.project},
                             "columnConfig": {"columns": [
                                 {"name": name, "statuses": [{"id": self.statuses[s]} for s in statuses]}
                                 for name, statuses in self.columns]}}
            on_board = {status for _, statuses in self.columns for status in statuses}
            jql = query.get("jql", [""])[0]
            rows = [self._issue(issue) for issue in self.issues.values()
                    if issue["status"] in on_board and self._matches(issue, jql)]
            return 200, self._page(rows, query, "issues")
        raise Problem(501, f"stub jira: unknown call {method} {path}")
