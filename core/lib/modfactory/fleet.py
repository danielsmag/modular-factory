"""A local list of installed factories: one queue and one dashboard.

Job state stays in each project. The fleet runs that project's own `factory`
binary and reads that project's `.factory/jobs/`.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from subprocess import CompletedProcess
from urllib.parse import unquote, urlsplit

from .util import CORE, FactoryError, Refused, find_repo, first_line, run, write_atomic

FILE = "fleet.toml"
SCHEMA = 1
SERVED = {".md": "text/plain", ".log": "text/plain"}
HEADER = (
    "# Checkouts where the factory is already installed. One job still belongs to one project.\n"
    "schema = 1\n"
)


def toml_string(value: str) -> str:
    escaped: str = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class Project:
    def __init__(self, path: Path) -> None:
        self.path: Path = path

    @property
    def binary(self) -> Path:
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
        raise Refused("no fleet.toml here. Run `factory fleet init` in the directory that should list your projects.")

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
            raise Refused(f"{repo} has no .factory/bin/factory. Install the factory there first.")
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

        `concurrency` overrides `fleet.toml`. It keeps that many `new` and `resume`
        rows, in project order, and marks the rest `hold`. `wait`, `skip` and
        `hold` do not take a slot. Omit both and each project keeps its own cap.
        """
        limit: int | None = self.concurrency if concurrency is None else parse_concurrency(concurrency, "--concurrency")
        rows: list[tuple[str, str, str, str, str, str]] = []
        errors: list[str] = []
        for project in self.projects:
            label: str = self.label(project)
            if not project.binary.is_file():
                errors.append(f"{label}: .factory/bin/factory is missing")
                continue
            try:
                result: CompletedProcess[str] = run([str(project.binary), "queue"], cwd=project.path, check=False)
            except FileNotFoundError:
                errors.append(f"{label}: .factory/bin/factory could not be run")
                continue
            if result.returncode != 0:
                detail: list[str] = (result.stderr or result.stdout).strip().splitlines()
                errors.append(f"{label}: {detail[-1] if detail else 'factory queue failed'}")
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
                missing: str = ".factory/bin/factory is missing"
                error = f"{error}; {missing}" if isinstance(error, str) and error else missing
            description: str = ""
            info: object = body.get("project")
            if isinstance(info, dict) and isinstance(info.get("description"), str):
                description = info["description"]
            jobs: object = body.get("jobs")
            items.append({
                "id": label,
                "name": project.path.name,
                "path": str(project.path),
                "description": description,
                "error": error,
                "jobs": jobs if isinstance(jobs, list) else [],
            })
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


def handler(file: Path) -> type[BaseHTTPRequestHandler]:
    page: bytes = (CORE / "fleet.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "factory-fleet"

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


def serve(file: Path, port: int = 4747, open_browser: bool = True, say: Callable[[str], None] = print) -> None:
    try:
        server: ThreadingHTTPServer = ThreadingHTTPServer(("127.0.0.1", port), handler(file))
    except OSError as error:
        raise Refused(f"port {port} can't be used ({error.strerror}). Pick another with --port.") from error
    url: str = f"http://127.0.0.1:{server.server_address[1]}/"
    say(f"factory fleet: {url}  (read-only, this machine only; Ctrl C stops it)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("")
    finally:
        server.server_close()
