"""The dashboard server: every subscribed project, or one checkout that is not subscribed.

`start_dashboard` and `stop_dashboard` run that server in the background.
"""

from __future__ import annotations

import argparse
import json
import os
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
from typing import TextIO
from urllib.parse import quote, unquote, urlsplit

from .. import activity
from ..config import Config
from ..config import load as load_config
from ..util import FactoryError, Refused, dashboard_page, user_dir
from .paths import FILE, SERVED
from .project import Project
from .registry import Fleet

QUEUE_TTL_SECONDS: float = 15.0
STATE_NAME: str = "dashboard.json"


def queue_payload(
    rows: list[tuple[str, str, str, str, str, str]], errors: list[str],
) -> dict[str, object]:
    items: list[dict[str, str]] = []
    for project, action, issue, job, step, note in rows:
        items.append({
            "project": project, "action": action, "issue": issue, "job": job, "step": step, "note": note,
        })
    return {"rows": items, "errors": errors}


class QueueCache:
    """One queue result, reused so the open page does not ask GitHub on every tick."""

    def __init__(self, load: Callable[[], Fleet], ttl: float = QUEUE_TTL_SECONDS) -> None:
        self._load: Callable[[], Fleet] = load
        self._ttl: float = ttl
        self._lock: threading.Lock = threading.Lock()
        self._at: float | None = None
        self._body: dict[str, object] = {"rows": [], "errors": []}

    def get(self) -> dict[str, object]:
        with self._lock:
            now: float = time.monotonic()
            if self._at is not None and now - self._at < self._ttl:
                return self._body
            fleet: Fleet = self._load()
            rows, errors = fleet.queue()
            self._body = queue_payload(rows, errors)
            self._at = time.monotonic()
            return self._body


def solo(repo: Path) -> Fleet:
    """One checkout, not written to a fleet file."""
    resolved: Path = repo.resolve()
    return Fleet(resolved / FILE, [Project(resolved)])


def _handler(load: Callable[[], Fleet]) -> type[BaseHTTPRequestHandler]:
    page: bytes = dashboard_page()
    cache: QueueCache = QueueCache(load)

    class Handler(BaseHTTPRequestHandler):
        server_version = "mfactory-fleet"

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
            path: str = unquote(urlsplit(self.path).path)
            try:
                fleet: Fleet = load()
                if path in ("/", "/index.html"):
                    self.send(page, "text/html")
                    return
                if path == "/projects.json":
                    self.send(json.dumps(fleet.snapshot()), "application/json")
                    return
                if path == "/queue.json":
                    self.send(json.dumps(cache.get()), "application/json")
                    return
                if path.startswith("/projects/"):
                    rest: str = path.removeprefix("/projects/")
                    ident, separator, job_path = rest.partition("/jobs/")
                    project: Project | None = fleet.project(ident) if separator else None
                    if project is not None and job_path.endswith("/activity"):
                        body: dict[str, object] | None = job_activity_response(project, job_path[: -len("/activity")])
                        if body is None:
                            self.send("not found", "text/plain", HTTPStatus.NOT_FOUND)
                            return
                        self.send(json.dumps(body), "application/json")
                        return
                    if project is not None:
                        root: Path = project.jobs_dir.resolve()
                        target: Path = (root / job_path).resolve()
                        if target.is_relative_to(root) and target.is_file() and target.suffix in SERVED:
                            self.send(target.read_bytes(), SERVED[target.suffix])
                            return
            except FactoryError as error:
                self.send(json.dumps({"error": str(error)}), "application/json", HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send("not found", "text/plain", HTTPStatus.NOT_FOUND)

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
    return _handler(lambda: Fleet.load(file))


def one_handler(repo: Path) -> type[BaseHTTPRequestHandler]:
    group: Fleet = solo(repo)
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


def running() -> RunningDashboard | None:
    path: Path = state_file()
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
    load: Callable[[], Fleet], port: int, open_browser: bool, say: Callable[[str], None],
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
    _listen(lambda: Fleet.load(file), port, open_browser, say, project, "mfactory fleet")


def serve_one(repo: Path, port: int = 4747, open_browser: bool = True, say: Callable[[str], None] = print) -> None:
    group: Fleet = solo(repo)
    label: str = group.label(group.projects[0])
    _listen(lambda: group, port, open_browser, say, label, "mfactory dashboard")


def dashboard_argv(args: argparse.Namespace) -> list[str]:
    script: Path = Path(sys.argv[0]).resolve()
    argv: list[str] = [sys.executable, str(script), "dashboard", "--port", str(args.port)]
    if args.no_open:
        argv.append("--no-open")
    if args.all:
        argv.append("--all")
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
