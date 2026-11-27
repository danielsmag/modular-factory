"""The dashboard server: every project in the workspace, or one checkout that is not listed.

`start_dashboard` and `stop_dashboard` run that server in the background.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import activity, config
from ..config import Config
from ..config import load as load_config
from ..service import tracker as trackers
from ..service.gh import GitHub
from ..service.tracker.base import key_kind
from ..service.tracker.jira_api import Client
from ..util import FactoryError, Refused, dashboard_page, user_dir
from . import feeds
from .paths import FILE, SERVED
from .project import Project
from .queue import QueueMeta
from .registry import Workspace

QUEUE_TTL_SECONDS: float = 15.0
STATE_NAME: str = "dashboard.json"
# An issue number, or a key such as PROJ-123 on a tracker that names its issues.
ISSUE_KEY: re.Pattern[str] = re.compile(r"[1-9][0-9]{0,9}|[A-Za-z][A-Za-z0-9_]{1,31}-[1-9][0-9]{0,9}")
# A Jira board's id.
BOARD_ID: re.Pattern[str] = re.compile(r"[1-9][0-9]{0,9}")
JIRA_OFF: str = ('Jira is off for this project. Add "jira" to [tracker] kind and set [tracker.jira] site '
                 "and board in factory.toml.")
NO_CREDENTIALS: str = ("Jira boards are read through Jira's REST API, with the account and token in the "
                       "dashboard's environment. Set {names} in a shell, then run `mfactory stop` and "
                       "`mfactory start dashboard` from it.")


def _web_url(value: object) -> str | None:
    return feeds.web_url(value)


def _text(value: object) -> str | None:
    """A string from GitHub, redacted like agent activity. None for anything else."""
    return activity.redact(value) if isinstance(value, str) else None


def _login(value: object) -> str | None:
    """A person as the page shows them: by name on a tracker whose logins are ids, else by login."""
    if not isinstance(value, dict):
        return None
    return _text(value.get("name")) or _text(value.get("login"))


def queue_payload(
    rows: list[tuple[str, str, str, str, str, str]], metas: list[QueueMeta], errors: list[str],
) -> dict[str, object]:
    """`/queue.json`. Only the list fields of each issue go out, redacted."""
    items: list[dict[str, object]] = []
    for (project, action, issue, job, step, note), meta in zip(rows, metas, strict=True):
        items.append({
            "project": project, "action": action, "issue": issue, "job": job, "step": step,
            "note": activity.redact(note),
            "title": _text(meta.title),
            "issue_url": _web_url(meta.issue_url),
            "created_at": _text(meta.created_at), "updated_at": _text(meta.updated_at),
            "author": _text(meta.author), "labels": [activity.redact(name) for name in meta.labels],
        })
    return {"rows": items, "errors": errors}


def issue_payload(raw: object) -> dict[str, object]:
    """The issue drawer's whitelisted fields, every string redacted."""
    issue: dict[str, object] = raw if isinstance(raw, dict) else {}
    raw_labels: object = issue.get("labels")
    labels: list[str] = []
    for label in raw_labels if isinstance(raw_labels, list) else []:
        name: str | None = _text(label.get("name")) if isinstance(label, dict) else None
        if name is not None:
            labels.append(name)
    raw_comments: object = issue.get("comments")
    comments: list[dict[str, object]] = []
    for item in raw_comments if isinstance(raw_comments, list) else []:
        comment: dict[str, object] = item if isinstance(item, dict) else {}
        comments.append({
            "author": _login(comment.get("author")),
            "created_at": _text(comment.get("createdAt")),
            "body": _text(comment.get("body")) or "",
        })
    number: object = issue.get("number")
    return {
        "number": number if isinstance(number, int | str) and not isinstance(number, bool) else None,
        "title": _text(issue.get("title")),
        "url": _web_url(issue.get("url")),
        "state": _text(issue.get("state")),
        "author": _login(issue.get("author")),
        "labels": labels,
        "created_at": _text(issue.get("createdAt")),
        "updated_at": _text(issue.get("updatedAt")),
        "body": _text(issue.get("body")) or "",
        "comment_count": len(comments),
        "comments": comments,
    }


def _reader(project: Project, ref: str) -> trackers.Tracker:
    """The tracker that shows issue `ref`: the project's own, or GitHub for a number in a project without it.

    Pull requests are always on GitHub, so the Board lists a repository's issues whatever its tracker.
    """
    tracker = trackers.for_repo(project.path)
    if key_kind(ref) == "github" and all(member.kind != "github" for member in tracker.members):
        return trackers.github_reader(project.path)
    return tracker.of(ref)


def issue_response(project: Project, ref: str) -> tuple[HTTPStatus, dict[str, object]]:
    """One issue of a project, read from its tracker on every call."""
    try:
        tracker: trackers.Tracker = _reader(project, ref)
        number: trackers.Key = tracker.key(ref)
    except FactoryError:
        return HTTPStatus.NOT_FOUND, {"error": f"{ref} is not an issue of this project"}
    shown: str = tracker.ref(number)
    try:
        raw: object = tracker.issue_details(number)
    except FactoryError as error:
        lines: list[str] = str(error).strip().splitlines()
        reason: str = lines[-1] if lines else "gh failed"
        return HTTPStatus.BAD_GATEWAY, {"error": f"Could not load issue {shown} from {tracker.label}: {reason}"}
    except json.JSONDecodeError:
        return HTTPStatus.BAD_GATEWAY, {"error": f"Could not load issue {shown} from {tracker.label}: "
                                                 "gh printed no JSON"}
    return HTTPStatus.OK, issue_payload(raw)


class QueueCache:
    """One queue result, reused so the open page does not ask GitHub on every tick."""

    def __init__(self, load: Callable[[], Workspace], ttl: float = QUEUE_TTL_SECONDS) -> None:
        self._load: Callable[[], Workspace] = load
        self._ttl: float = ttl
        self._lock: threading.Lock = threading.Lock()
        self._at: float | None = None
        self._body: dict[str, object] = {"rows": [], "errors": []}

    def get(self) -> dict[str, object]:
        with self._lock:
            now: float = time.monotonic()
            if self._at is not None and now - self._at < self._ttl:
                return self._body
            workspace: Workspace = self._load()
            rows, metas, errors = workspace.queue()
            self._body = queue_payload(rows, metas, errors)
            self._at = time.monotonic()
            return self._body


class Feeds:
    """The Board's Jira and GitHub blocks for each project, read through a short cache.

    Each project's own settings say which Jira site to read and which environment variables hold the
    account and its token. A failure is an answer too: the page shows it in that block only.
    """

    def __init__(self) -> None:
        self.cache: feeds.FeedCache = feeds.FeedCache(ttl=60, error_ttl=15)
        self.slow: feeds.FeedCache = feeds.FeedCache(ttl=600, error_ttl=15)

    def _jira(self, project: Project) -> tuple[Client, int] | str:
        """A REST client for the project's Jira site and its board, or why there is none to read.

        Boards are read through REST whenever the account and token are set, whatever `via` says: the
        Atlassian CLI cannot read a board, and the dashboard's own environment is the one that counts.
        """
        cfg: Config = load_config(project.path)
        if "jira" not in config.tracker_kinds(cfg["tracker"]["kind"]):
            return JIRA_OFF
        settings: dict[str, Any] = cfg["tracker"]["jira"]
        missing: list[str] = [settings[key] for key in ("email_env", "token_env") if not os.environ.get(settings[key])]
        if missing:
            return NO_CREDENTIALS.format(names=" and ".join(missing))
        client: Client = Client(str(settings["site"]), settings["email_env"], settings["token_env"])
        return client, int(settings.get("board") or 0)

    def jira_boards(self, project: Project, label: str, fresh: bool) -> Reply:
        found: tuple[Client, int] | str = self._jira(project)
        if found == JIRA_OFF:
            return json_reply({"off": JIRA_OFF})
        if isinstance(found, str):
            return json_reply({"site": None, "configured": None, "boards": [], "note": found})
        client, board = found
        return self._answer("Could not list the Jira boards", lambda: self.slow.get(
            (label, "jira", "boards"), lambda: feeds.jira_boards(client, board), fresh))

    def jira_board(self, project: Project, label: str, board_id: int, fresh: bool) -> Reply:
        found: tuple[Client, int] | str = self._jira(project)
        if isinstance(found, str):
            return json_reply({"off": found}) if found == JIRA_OFF else json_reply({"note": found})
        client, _ = found
        return self._answer(f"Could not load board {board_id} from Jira", lambda: self.cache.get(
            (label, "jira", board_id), lambda: feeds.jira_board(client, board_id), fresh))

    def github_issues(self, project: Project, label: str, fresh: bool) -> Reply:
        gh: GitHub = GitHub(project.path)
        reader: trackers.GitHubTracker = trackers.github_reader(project.path)

        def me() -> str:
            return self.slow.get((label, "github", "me"), reader.operator)

        def repo_url() -> str | None:
            return self.slow.get((label, "github", "repo"), gh.repo_url)
        return self._answer("Could not load the issues from GitHub", lambda: self.cache.get(
            (label, "github", "issues"), lambda: feeds.github_issues(gh, me, repo_url), fresh))

    @staticmethod
    def _answer(what: str, read: Callable[[], dict[str, Any]]) -> Reply:
        """200 with what `read` returns, or 502 with why it failed. Never the failure's traceback."""
        try:
            return json_reply(read())
        except (FactoryError, subprocess.SubprocessError, OSError, ValueError) as error:
            lines: list[str] = str(error).strip().splitlines()
            why: str = activity.redact(lines[-1]) if lines else type(error).__name__
            return json_reply({"error": f"{what}: {why}"}, HTTPStatus.BAD_GATEWAY)


def solo(repo: Path) -> Workspace:
    """One checkout, not written to a workspace file."""
    resolved: Path = repo.resolve()
    return Workspace(resolved / FILE, [Project(resolved)])


# One route's answer: status, body and content type.
Reply = tuple[HTTPStatus, bytes | str, str]
# A route reads the workspace, the path's named groups and the query string.
Route = tuple[re.Pattern[str], Callable[[Workspace, re.Match[str], dict[str, list[str]]], Reply]]
JSON: str = "application/json"
NOT_FOUND: Reply = (HTTPStatus.NOT_FOUND, "not found", "text/plain")


def json_reply(body: object, status: HTTPStatus = HTTPStatus.OK) -> Reply:
    return status, json.dumps(body), JSON


def dispatch(routes: list[Route], workspace: Workspace, path: str, query: dict[str, list[str]]) -> Reply:
    """The answer of the first route whose pattern matches the whole path. 404 when none does."""
    for pattern, answer in routes:
        found: re.Match[str] | None = pattern.fullmatch(path)
        if found:
            return answer(workspace, found, query)
    return NOT_FOUND


def _routes(page: bytes, cache: QueueCache, blocks: Feeds | None = None) -> list[Route]:
    """Every page and JSON route the dashboard answers. Each one only reads."""
    feed: Feeds = blocks or Feeds()

    def fresh(query: dict[str, list[str]]) -> bool:
        return query.get("fresh", [""])[0] == "1"

    def jira_boards(workspace: Workspace, found: re.Match[str], query: dict[str, list[str]]) -> Reply:
        project: Project | None = workspace.project(found["project"])
        return NOT_FOUND if project is None else feed.jira_boards(project, found["project"], fresh(query))

    def jira_board(workspace: Workspace, found: re.Match[str], query: dict[str, list[str]]) -> Reply:
        project: Project | None = workspace.project(found["project"])
        if project is None or not BOARD_ID.fullmatch(found["board"]):
            return NOT_FOUND
        return feed.jira_board(project, found["project"], int(found["board"]), fresh(query))

    def github_issues(workspace: Workspace, found: re.Match[str], query: dict[str, list[str]]) -> Reply:
        project: Project | None = workspace.project(found["project"])
        return NOT_FOUND if project is None else feed.github_issues(project, found["project"], fresh(query))

    def index(workspace: Workspace, found: re.Match[str], query: dict[str, list[str]]) -> Reply:
        return HTTPStatus.OK, page, "text/html"

    def projects(workspace: Workspace, found: re.Match[str], query: dict[str, list[str]]) -> Reply:
        return json_reply(workspace.snapshot())

    def queue(workspace: Workspace, found: re.Match[str], query: dict[str, list[str]]) -> Reply:
        return json_reply(cache.get())

    def issue(workspace: Workspace, found: re.Match[str], query: dict[str, list[str]]) -> Reply:
        project: Project | None = workspace.project(found["project"])
        if project is None or not ISSUE_KEY.fullmatch(found["ref"]):
            return NOT_FOUND
        status, body = issue_response(project, found["ref"])
        return json_reply(body, status)

    def activity(workspace: Workspace, found: re.Match[str], query: dict[str, list[str]]) -> Reply:
        project: Project | None = workspace.project(found["project"])
        body: dict[str, object] | None = job_activity_response(project, found["job"]) if project else None
        return NOT_FOUND if body is None else json_reply(body)

    def job_file(workspace: Workspace, found: re.Match[str], query: dict[str, list[str]]) -> Reply:
        project: Project | None = workspace.project(found["project"])
        if project is None:
            return NOT_FOUND
        root: Path = project.jobs_dir.resolve()
        target: Path = (root / found["path"]).resolve()
        if target.is_relative_to(root) and target.is_file() and target.suffix in SERVED:
            return HTTPStatus.OK, target.read_bytes(), SERVED[target.suffix]
        return NOT_FOUND

    return [
        (re.compile(r"/(?:index\.html)?"), index),
        (re.compile(r"/projects\.json"), projects),
        (re.compile(r"/queue\.json"), queue),
        (re.compile(r"/projects/(?P<project>[^/]+)/issues/(?P<ref>.+)"), issue),
        (re.compile(r"/projects/(?P<project>[^/]+)/jira/boards"), jira_boards),
        (re.compile(r"/projects/(?P<project>[^/]+)/jira/boards/(?P<board>[^/]+)"), jira_board),
        (re.compile(r"/projects/(?P<project>[^/]+)/github/issues"), github_issues),
        (re.compile(r"/projects/(?P<project>[^/]+)/jobs/(?P<job>.+)/activity"), activity),
        (re.compile(r"/projects/(?P<project>[^/]+)/jobs/(?P<path>.+)"), job_file),
    ]


def _handler(load: Callable[[], Workspace]) -> type[BaseHTTPRequestHandler]:
    routes: list[Route] = _routes(dashboard_page(), QueueCache(load))

    class Handler(BaseHTTPRequestHandler):
        server_version = "mfactory-workspace"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def send(self, body: bytes | str, kind: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            data: bytes = body if isinstance(body, bytes) else body.encode()
            self.send_response(status)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            host: str = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                self.send("forbidden", "text/plain", HTTPStatus.FORBIDDEN)
                return
            parts = urlsplit(self.path)
            try:
                status, body, kind = dispatch(routes, load(), unquote(parts.path), parse_qs(parts.query))
            except FactoryError as error:
                status, body, kind = json_reply({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send(body, kind, status)

    return Handler


def job_activity_response(project: Project, job_id: str) -> dict[str, object] | None:
    """Redacted agent activity for one job. None when the job id leaves the jobs folder."""
    if not job_id or "/" in job_id or job_id in {".", ".."}:
        return None
    root: Path = project.jobs_dir.resolve()
    job_dir: Path = (project.jobs_dir / job_id).resolve()
    if not job_dir.is_relative_to(root) or not (job_dir / "job.json").is_file():
        return None
    try:
        loaded: object = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"runs": [], "error": "job.json unreadable"}
    if not isinstance(loaded, dict):
        return {"runs": [], "error": "job.json unreadable"}
    cfg: Config = load_config(project.path)
    return activity.job_activity(cfg, loaded, job_dir)


def handler(file: Path) -> type[BaseHTTPRequestHandler]:
    return _handler(lambda: Workspace.load(file))


def one_handler(repo: Path) -> type[BaseHTTPRequestHandler]:
    group: Workspace = solo(repo)
    return _handler(lambda: group)


@dataclass
class RunningDashboard:
    pid: int
    port: int
    url: str


def state_file() -> Path:
    return user_dir() / STATE_NAME


def alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running(path: Path | None = None) -> RunningDashboard | None:
    """The dashboard the state file names, when its pid is live. A stale file is deleted."""
    path = path or state_file()
    if not path.is_file():
        return None
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    pid: object = raw.get("pid")
    port: object = raw.get("port")
    url: object = raw.get("url")
    if not isinstance(pid, int) or not isinstance(port, int) or not isinstance(url, str) or not alive(pid):
        path.unlink(missing_ok=True)
        return None
    return RunningDashboard(pid, port, url)


def claim(port: int, url: str) -> None:
    other: RunningDashboard | None = running()
    if other is not None and other.pid != os.getpid():
        raise Refused(f"dashboard already running at {other.url}. Stop it with `mfactory stop`.")
    path: Path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, int | str] = {"pid": os.getpid(), "port": port, "url": url}
    path.write_text(json.dumps(payload) + "\n")


def release() -> None:
    path: Path = state_file()
    if not path.is_file():
        return
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(raw, dict) and raw.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


class DashboardServer(ThreadingHTTPServer):
    """`HTTPServer.server_bind` reverse-resolves the listen address.

    `socket.getfqdn` blocks on the macOS runner for longer than `start` waits,
    so the process is alive and the dashboard is reported as not started.
    """

    def server_bind(self) -> None:
        super(HTTPServer, self).server_bind()
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])


def _listen(
    load: Callable[[], Workspace], port: int, open_browser: bool, say: Callable[[str], None],
    project: str | None, label: str,
) -> None:
    try:
        server: DashboardServer = DashboardServer(("127.0.0.1", port), _handler(load))
    except OSError as error:
        raise Refused(f"port {port} can't be used ({error.strerror}). Pick another with --port.") from error
    url: str = f"http://127.0.0.1:{server.server_address[1]}/"
    if project:
        url = f"{url}?project={quote(project, safe='')}"
    try:
        claim(server.server_address[1], url)
        say(f"{label}: {url}  (read-only, this machine only; Ctrl C stops it)")
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            say("")
    finally:
        release()
        server.server_close()


def serve(
    file: Path, port: int = 4747, open_browser: bool = True,
    say: Callable[[str], None] = print, project: str | None = None,
) -> None:
    _listen(lambda: Workspace.load(file), port, open_browser, say, project, "mfactory dashboard")


def serve_one(repo: Path, port: int = 4747, open_browser: bool = True, say: Callable[[str], None] = print) -> None:
    group: Workspace = solo(repo)
    label: str = group.label(group.projects[0])
    _listen(lambda: group, port, open_browser, say, label, "mfactory dashboard")


def dashboard_argv(args: argparse.Namespace) -> list[str]:
    script: Path = Path(sys.argv[0]).resolve()
    argv: list[str] = [sys.executable, str(script), "dashboard", "--port", str(args.port)]
    if args.no_open:
        argv.append("--no-open")
    return argv


def start_dashboard(args: argparse.Namespace) -> int:
    current: RunningDashboard | None = running()
    if current is not None:
        raise Refused(f"dashboard already running at {current.url}. Stop it with `mfactory stop`.")
    home: Path = user_dir()
    home.mkdir(parents=True, exist_ok=True)
    log_path: Path = home / "dashboard.log"
    log: TextIO = log_path.open("w", encoding="utf-8")
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        dashboard_argv(args), start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    )
    log.close()
    deadline: float = time.monotonic() + 5
    while time.monotonic() < deadline:
        found: RunningDashboard | None = running()
        if found is not None and found.pid == proc.pid:
            print(f"mfactory dashboard: {found.url}")
            return 0
        if proc.poll() is not None:
            detail: str = log_path.read_text().strip()
            if detail.startswith("mfactory: "):
                detail = detail.removeprefix("mfactory: ").strip()
            raise Refused(detail or "dashboard exited before it was ready")
        time.sleep(0.05)
    proc.kill()
    raise Refused("dashboard did not start")


def stop_dashboard() -> int:
    current: RunningDashboard | None = running()
    if current is None:
        print("dashboard is not running")
        return 0
    os.kill(current.pid, signal.SIGTERM)
    deadline: float = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not alive(current.pid):
            state_file().unlink(missing_ok=True)
            print(f"stopped {current.url}")
            return 0
        time.sleep(0.05)
    raise Refused(f"dashboard {current.pid} did not stop")
