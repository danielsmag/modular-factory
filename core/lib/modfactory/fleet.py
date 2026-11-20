"""A local list of installed factories: one queue and one dashboard.

Job state stays in each project. The fleet runs that project's own `factory`
binary and reads that project's `.factory/jobs/`.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import tomllib
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from subprocess import CompletedProcess
from urllib.parse import quote, unquote, urlsplit

from .util import CORE, FactoryError, Refused, find_repo, first_line, run, write_atomic

FILE = "fleet.toml"
SCHEMA = 1
QUEUE_TTL_SECONDS: float = 15.0
SERVED: dict[str, str] = {".md": "text/plain", ".log": "text/plain"}
HEADER: str = (
    "# Checkouts where the factory is already installed. One job still belongs to one project.\n"
    "schema = 1\n"
)


def user_dir() -> Path:
    """This user's factory directory. `FACTORY_HOME` overrides `~/.mfactory`."""
    chosen: str | None = os.environ.get("FACTORY_HOME")
    if chosen:
        return Path(chosen).expanduser()
    return Path.home() / ".mfactory"


def user_file() -> Path:
    """The fleet file for this user. `subscribe` creates it, and the directory with it."""
    return user_dir() / FILE


def toml_string(value: str) -> str:
    escaped: str = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class Project:
    def __init__(self, path: Path) -> None:
        self.path: Path = path

    @property
    def binary(self) -> Path:
        current: Path = self.path / ".factory" / "bin" / "mfactory"
        if current.is_file():
            return current
        return self.path / ".factory" / "bin" / "factory"

    @property
    def jobs_dir(self) -> Path:
        return self.path / ".factory" / "jobs"


def labels_for(projects: list[Project]) -> dict[Path, str]:
    """Folder name, plus a short hash when two checkouts share that name."""
    names: list[str] = [project.path.name for project in projects]
    labels: dict[Path, str] = {}
    for project in projects:
        label: str = project.path.name
        if names.count(project.path.name) > 1:
            digest: str = hashlib.sha256(str(project.path).encode()).hexdigest()[:4]
            label = f"{project.path.name}-{digest}"
        labels[project.path] = label
    return labels


def default_concurrency() -> int | None:
    """Fleet-wide cap shipped in this factory's defaults.toml. None when that copy has no [fleet] table."""
    path: Path = CORE / "defaults.toml"
    parsed: object = tomllib.loads(path.read_text())
    if not isinstance(parsed, dict):
        return None
    table: object = parsed.get("fleet")
    if not isinstance(table, dict) or "concurrency" not in table:
        return None
    return parse_concurrency(table["concurrency"], f"{path}: [fleet].concurrency")


def parse_concurrency(value: object, where: str) -> int:
    """A fleet-wide cap: how many `new` and `resume` rows survive across every project."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Refused(f"{where} must be a whole number of at least 1 (got {value})")
    return value


def cap_rows(
    rows: list[tuple[str, str, str, str, str, str]], limit: int,
) -> list[tuple[str, str, str, str, str, str]]:
    """Keep the first `limit` actionable rows. Later `new` and `resume` rows become `hold`."""
    kept: int = 0
    capped: list[tuple[str, str, str, str, str, str]] = []
    for project, action, issue, job, step, note in rows:
        if action in ("new", "resume"):
            if kept < limit:
                kept += 1
            else:
                action = "hold"
                note = f"fleet concurrency {limit} reached"
        capped.append((project, action, issue, job, step, note))
    return capped


class Fleet:
    def __init__(self, file: Path, projects: list[Project], concurrency: int | None = None) -> None:
        self.file: Path = file
        self.projects: list[Project] = projects
        self.concurrency: int | None = concurrency

    def label(self, project: Project) -> str:
        return labels_for(self.projects)[project.path]

    def project(self, label: str) -> Project | None:
        for item in self.projects:
            if self.label(item) == label:
                return item
        return None

    @staticmethod
    def init(directory: Path) -> Path:
        file: Path = directory / FILE
        if file.exists():
            raise Refused(f"{file} already exists")
        fleet: Fleet = Fleet(file, [])
        fleet.save()
        return file

    @staticmethod
    def find(start: Path | None = None) -> Fleet:
        return Fleet.locate(start, create=False)

    @staticmethod
    def locate(start: Path | None = None, create: bool = False) -> Fleet:
        """The fleet in effect: FACTORY_FLEET, then a fleet.toml above here, then the user file.

        `create` writes the user file when the first two are absent. It never creates a file
        for a missing FACTORY_FLEET path.
        """
        override: str | None = os.environ.get("FACTORY_FLEET")
        if override:
            file: Path = Path(override).expanduser()
            if not file.is_file():
                raise Refused(f"{file} does not exist")
            return Fleet.load(file)
        cwd: Path = (start or Path.cwd()).resolve()
        for directory in [cwd, *cwd.parents]:
            candidate: Path = directory / FILE
            if candidate.is_file():
                return Fleet.load(candidate)
        user: Path = user_file()
        if user.is_file():
            return Fleet.load(user)
        if create:
            user.parent.mkdir(parents=True, exist_ok=True)
            created: Fleet = Fleet(user, [])
            created.save()
            return created
        raise Refused("no fleet. Run `mfactory fleet subscribe` inside a project.")

    @staticmethod
    def load(file: Path) -> Fleet:
        try:
            parsed: object = tomllib.loads(file.read_text())
        except tomllib.TOMLDecodeError as error:
            raise Refused(f"{file} is not valid TOML: {error}") from error
        if not isinstance(parsed, dict):
            raise Refused(f"{file} is not valid TOML")
        schema: object = parsed.get("schema")
        if schema != SCHEMA:
            raise Refused(f"{file} is schema {schema}; this factory reads schema {SCHEMA}")
        raw_limit: object = parsed.get("concurrency")
        limit: int | None = None if raw_limit is None else parse_concurrency(raw_limit, f"{file}: concurrency")
        raw: object = parsed.get("projects") or []
        if not isinstance(raw, list):
            raise Refused(f"{file}: projects must be a list of tables")
        projects: list[Project] = []
        for row in raw:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                raise Refused(f"{file}: each [[projects]] entry needs a path")
            projects.append(Project(Path(row["path"]).expanduser().resolve()))
        return Fleet(file, projects, limit)

    def save(self) -> None:
        lines: list[str] = [HEADER.rstrip("\n")]
        if self.concurrency is not None:
            lines.append(f"concurrency = {self.concurrency}")
        lines.append("")
        for project in self.projects:
            lines.append("[[projects]]")
            lines.append(f"path = {toml_string(str(project.path))}")
            lines.append("")
        write_atomic(self.file, "\n".join(lines).rstrip("\n") + "\n")

    def add(self, path: Path) -> Project:
        repo: Path = find_repo(path)
        project: Project = Project(repo)
        if not project.binary.is_file():
            raise Refused(f"{repo} has no .factory/bin/mfactory. Install the factory there first.")
        if any(existing.path == project.path for existing in self.projects):
            raise Refused(f"{repo} is already in {self.file}")
        self.projects.append(project)
        self.save()
        return project

    def remove(self, ref: str) -> Project:
        wanted: Path = Path(ref).expanduser()
        resolved: Path | None = wanted.resolve() if wanted.exists() else None
        matches: list[Project] = []
        for project in self.projects:
            same_path: bool = project.path == resolved or str(project.path) == str(wanted)
            same_name: bool = ref == self.label(project) or ref == project.path.name
            if same_path or same_name:
                matches.append(project)
        if not matches:
            raise Refused(f"no project matches {ref!r}")
        if len(matches) > 1:
            raise Refused(f"{ref!r} matches more than one project; pass the path")
        chosen: Project = matches[0]
        self.projects.remove(chosen)
        self.save()
        return chosen

    def queue(self, concurrency: int | None = None) -> tuple[list[tuple[str, str, str, str, str, str]], list[str]]:
        """Rows of `project action issue job step note`, and one error string per project that failed.

        Each project has already applied its own `[run].concurrency`. This cap is
        across projects: `--concurrency`, then `fleet.toml`, then `[fleet].concurrency`
        in defaults.toml. It keeps that many `new` and `resume` rows, in project
        order, and marks the rest `hold`. `wait`, `skip` and `hold` do not take a slot.
        """
        limit: int | None = self.concurrency if self.concurrency is not None else default_concurrency()
        if concurrency is not None:
            limit = parse_concurrency(concurrency, "--concurrency")
        rows: list[tuple[str, str, str, str, str, str]] = []
        errors: list[str] = []
        for project in self.projects:
            label: str = self.label(project)
            if not project.binary.is_file():
                errors.append(f"{label}: .factory/bin/mfactory is missing")
                continue
            try:
                result: CompletedProcess[str] = run([str(project.binary), "queue"], cwd=project.path, check=False)
            except FileNotFoundError:
                errors.append(f"{label}: .factory/bin/mfactory could not be run")
                continue
            if result.returncode != 0:
                detail: list[str] = (result.stderr or result.stdout).strip().splitlines()
                errors.append(f"{label}: {detail[-1] if detail else 'mfactory queue failed'}")
                continue
            for line in result.stdout.splitlines():
                cells: list[str] = line.split("  ", 4)
                if len(cells) != 5:
                    continue
                action, issue, job, step, note = cells
                rows.append((label, action, issue, job, step, note))
        if limit is not None:
            rows = cap_rows(rows, limit)
        return rows, errors

    def snapshot(self) -> dict[str, object]:
        items: list[dict[str, object]] = []
        for project in self.projects:
            label: str = self.label(project)
            body: dict[str, object] = read_jobs(project)
            error: object = body.get("error")
            if not project.binary.is_file():
                missing: str = ".factory/bin/mfactory is missing"
                error = f"{error}; {missing}" if isinstance(error, str) and error else missing
            description: str = ""
            info: object = body.get("project")
            if isinstance(info, dict) and isinstance(info.get("description"), str):
                description = info["description"]
            jobs: object = body.get("jobs")
            version: str = body.get("version") if isinstance(body.get("version"), str) else ""
            generated_at: str = body.get("generated_at") if isinstance(body.get("generated_at"), str) else ""
            tools: list[str] = []
            tools_raw: object = body.get("tools")
            if isinstance(tools_raw, list):
                for tool in tools_raw:
                    if isinstance(tool, str):
                        tools.append(tool)
            labels: dict[str, str] = {}
            labels_raw: object = body.get("labels")
            if isinstance(labels_raw, dict):
                for key, value in labels_raw.items():
                    if isinstance(key, str) and isinstance(value, str):
                        labels[key] = value
            config_rows: list[dict[str, object]] | None
            config_error: str | None
            config_rows, config_error = read_config(project)
            if config_error:
                error = f"{error}; {config_error}" if isinstance(error, str) and error else config_error
            item: dict[str, object] = {
                "id": label,
                "name": project.path.name,
                "path": str(project.path),
                "description": description,
                "error": error,
                "jobs": jobs if isinstance(jobs, list) else [],
                "version": version,
                "generated_at": generated_at,
                "tools": tools,
                "labels": labels,
                "config": config_rows,
            }
            graph: dict[str, object] | None = read_graph(project)
            if graph is not None:
                item["graph"] = graph
            items.append(item)
        return {"projects": items}


def file_rows(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in SERVED:
            rows.append({
                "path": str(path.relative_to(root)),
                "size": path.stat().st_size,
                "first_line": first_line(path) if path.suffix == ".md" else None,
            })
    return rows


def read_graph(project: Project) -> dict[str, object] | None:
    """Pipeline for the fleet board. None when graph.json is missing or not a graph."""
    path: Path = project.jobs_dir / "graph.json"
    if not path.is_file():
        return None
    try:
        text: str = path.read_text()
    except OSError:
        return None
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    steps: object = loaded.get("steps")
    ends: object = loaded.get("end_states")
    if not isinstance(steps, list) or not isinstance(ends, list):
        return None
    for step in steps:
        if not isinstance(step, dict):
            return None
        name: object = step.get("name")
        edges: object = step.get("edges")
        if not isinstance(name, str) or not name or not isinstance(edges, list):
            return None
    return loaded


def read_config(project: Project) -> tuple[list[dict[str, object]] | None, str | None]:
    """Rows from ``config.json``. A missing file is ``(None, None)``. A bad file is ``(None, error)``."""
    path: Path = project.jobs_dir / "config.json"
    if not path.is_file():
        return None, None
    try:
        text: str = path.read_text()
    except OSError as error:
        return None, f"config.json: {error.strerror or 'could not be read'}"
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError as error:
        return None, f"config.json: {error}"
    if not isinstance(loaded, list):
        return None, "config.json is not a list"
    rows: list[dict[str, object]] = []
    for row in loaded:
        key: object = row.get("key") if isinstance(row, dict) else None
        source: object = row.get("source") if isinstance(row, dict) else None
        if not isinstance(row, dict) or not isinstance(key, str) or not isinstance(source, str) or "value" not in row:
            return None, "config.json is not a list of settings"
        rows.append({"key": key, "value": row["value"], "source": source})
    return rows, None


def read_jobs(project: Project) -> dict[str, object]:
    index_path: Path = project.jobs_dir / "index.json"
    if index_path.is_file():
        try:
            loaded: object = json.loads(index_path.read_text())
        except json.JSONDecodeError as error:
            return {"jobs": [], "error": f"index.json: {error}"}
        if isinstance(loaded, dict):
            result: dict[str, object] = dict(loaded)
            result["error"] = None
            return result
        return {"jobs": [], "error": "index.json is not an object"}
    jobs: list[dict[str, object]] = []
    if project.jobs_dir.is_dir():
        for child in sorted(project.jobs_dir.iterdir()):
            job_file: Path = child / "job.json"
            if not job_file.is_file():
                continue
            try:
                job: object = json.loads(job_file.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(job, dict):
                job["files"] = file_rows(child)
                jobs.append(job)
    return {"jobs": jobs, "project": {}, "error": None}


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

    def __init__(self, file: Path, ttl: float = QUEUE_TTL_SECONDS) -> None:
        self._file: Path = file
        self._ttl: float = ttl
        self._lock: threading.Lock = threading.Lock()
        self._at: float | None = None
        self._body: dict[str, object] = {"rows": [], "errors": []}

    def get(self) -> dict[str, object]:
        with self._lock:
            now: float = time.monotonic()
            if self._at is not None and now - self._at < self._ttl:
                return self._body
            fleet: Fleet = Fleet.load(self._file)
            rows, errors = fleet.queue()
            self._body = queue_payload(rows, errors)
            self._at = time.monotonic()
            return self._body


def handler(file: Path) -> type[BaseHTTPRequestHandler]:
    page: bytes = (CORE / "dashboard.html").read_bytes()
    cache: QueueCache = QueueCache(file)

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
                fleet: Fleet = Fleet.load(file)
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


def serve(
    file: Path, port: int = 4747, open_browser: bool = True,
    say: Callable[[str], None] = print, project: str | None = None,
) -> None:
    try:
        server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", port), handler(file))
    except OSError as error:
        raise Refused(f"port {port} can't be used ({error.strerror}). Pick another with --port.") from error
    url: str = f"http://127.0.0.1:{server.server_address[1]}/"
    if project:
        url = f"{url}?project={quote(project, safe='')}"
    say(f"mfactory fleet: {url}  (read-only, this machine only; Ctrl C stops it)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("")
    finally:
        server.server_close()
