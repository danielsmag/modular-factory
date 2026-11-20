"""`factory dashboard`: serve the read-only dashboard on this machine."""

import json
import os
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import config, graph
from .store import Store
from .util import CORE, FactoryError, Refused, user_dir

STATE_NAME: str = "dashboard.json"

SERVED = {".md": "text/plain", ".log": "text/plain"}


def handler(repo):
    page = (CORE / "dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "mfactory-dashboard"

        def log_message(self, *args):
            pass

        def send(self, body, kind, status=HTTPStatus.OK):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(status)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # The page shows text that came from GitHub issues; it may load nothing from elsewhere.
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                             "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # A page on another site could otherwise read job files through DNS rebinding.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in ("127.0.0.1", "localhost", "[::1]"):
                return self.send("forbidden", "text/plain", HTTPStatus.FORBIDDEN)
            path = unquote(urlsplit(self.path).path)
            try:
                if path in ("/", "/index.html"):
                    return self.send(page, "text/html")
                # The config is read on every request, so edits to factory.toml show up.
                cfg = config.load(repo)
                store = Store(cfg)
                if path == "/jobs/index.json":
                    return self.send(json.dumps(store.index()), "application/json")
                if path == "/jobs/graph.json":
                    return self.send(json.dumps(graph.as_json(cfg)), "application/json")
                if path == "/jobs/config.json":
                    return self.send(json.dumps(config.as_json(cfg)), "application/json")
                if path.startswith("/jobs/"):
                    root = cfg.jobs_dir.resolve()
                    target = (root / path.removeprefix("/jobs/")).resolve()
                    if target.is_relative_to(root) and target.is_file() and target.suffix in SERVED:
                        return self.send(target.read_bytes(), SERVED[target.suffix])
            except FactoryError as e:
                return self.send(json.dumps({"error": str(e)}), "application/json",
                                 HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send("not found", "text/plain", HTTPStatus.NOT_FOUND)

    return Handler


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


def serve(repo: Path, port: int = 4747, open_browser: bool = True, say: Callable[[str], None] = print) -> None:
    try:
        server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", port), handler(repo))
    except OSError as error:
        raise Refused(f"port {port} can't be used ({error.strerror}). Pick another with --port.") from error
    bound: int = server.server_address[1]
    url: str = f"http://127.0.0.1:{bound}/"
    try:
        claim(bound, url)
        say(f"mfactory dashboard: {url}  (read-only, this machine only; Ctrl C stops it)")
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            say("")
    finally:
        release()
        server.server_close()
