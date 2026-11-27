"""Job state on disk: `.factory/jobs/<id>/job.json` and the dashboard's index."""

import fcntl
import json
import re
from pathlib import Path

from . import config, graph, plan_usage
from .service import tracker as trackers
from .util import Refused, first_line, now, version, write_atomic

RUNNING_TYPES = {"agent", "checks", "review", "merge"}
# The shape of job.json and index.json that the dashboard reads. Raise it when a field changes meaning.
SCHEMA: int = 1


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = cfg.jobs_dir
        self._locks = {}

    def path(self, job_id):
        return self.dir / job_id

    def ids(self):
        if not self.dir.exists():
            return []
        return [p.name for p in sorted(self.dir.iterdir()) if (p / "job.json").exists()]

    def read(self, job_id):
        return json.loads((self.path(job_id) / "job.json").read_text())

    def all(self):
        return [self.read(job_id) for job_id in self.ids()]

    def find(self, ref):
        """A job id (`004-dark-mode`), its number as in the id (`004`), or its issue's key (`12` or `#12`)."""
        ref = str(ref)
        jobs = self.all()
        matches = [j for j in jobs if j["id"] == ref or j["id"].startswith(ref + "-")]
        if not matches:
            key = ref.lstrip("#").lower()
            matches = [j for j in jobs if str(j["issue"]).lower() == key]
        if not matches:
            raise Refused(f"no job matches {ref!r}. Jobs: {', '.join(self.ids()) or 'none'}")
        return matches[0]["id"]

    def open(self, ref):
        """Read a job for changing, holding its lock until this process exits."""
        job_id = self.find(ref)
        if job_id not in self._locks:
            handle = open(self.path(job_id) / ".lock", "w")  # noqa: SIM115
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise Refused(f"another factory command is working on {job_id}; "
                              "wait for it to finish") from None
            self._locks[job_id] = handle
        return self.read(job_id)

    def release(self):
        """Give up every lock this process holds. A command does this by exiting."""
        for handle in self._locks.values():
            handle.close()
        self._locks = {}

    def try_open(self, ref):
        try:
            return self.open(ref)
        except Refused:
            return None

    def new_id(self, title):
        """The next job number and id. Numbers only go up, so a removed job's id never comes back."""
        self.dir.mkdir(parents=True, exist_ok=True)
        counter = self.dir / "last_number"
        with open(self.dir / ".number.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                last = int(counter.read_text().strip())
            except (OSError, ValueError):
                last = 0
            numbers = [int(i.split("-")[0]) for i in self.ids()]
            number = max(last, *numbers, 0) + 1
            write_atomic(counter, f"{number}\n")
        words = re.findall(r"[a-z0-9]+", title.lower())[:4]
        return number, f"{number:03d}-{'-'.join(words) or 'issue'}"

    def save(self, job):
        job["schema"] = SCHEMA
        job["updated_at"] = now()
        self.path(job["id"]).mkdir(parents=True, exist_ok=True)
        write_atomic(self.path(job["id"]) / "job.json", json.dumps(job, indent=2) + "\n")
        self.write_index()

    # -- slots

    def _pipe(self, job):
        return self.cfg.pipeline_of(job)

    def holds_slot(self, job):
        step = self._pipe(job).steps.get(job["step"])
        return bool(step) and step["type"] in RUNNING_TYPES

    def running(self):
        """Jobs holding a concurrency slot, in the order they got it."""
        jobs = [j for j in self.all() if self.holds_slot(j)]
        return sorted(jobs, key=lambda j: (j.get("running_order") or 0, j["id"]))

    # -- dashboard data

    def files(self, job_id):
        root = self.path(job_id)
        rows = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in (".md", ".log"):
                rows.append({"path": str(path.relative_to(root)), "size": path.stat().st_size,
                             "first_line": first_line(path) if path.suffix == ".md" else None})
        return rows

    def index(self):
        cfg = self.cfg
        tracker = trackers.make(cfg)
        return {
            "schema": SCHEMA,
            "generated_at": now(),
            "version": version(),
            "project": cfg["project"],
            "repo": cfg.repo.name,
            "tools": cfg.tools,
            "concurrency": cfg["run"]["concurrency"],
            "labels": cfg["labels"],
            # Where jobs come from, for the page's wording. Read from factory.toml; the tracker is not asked.
            # `tracker` is the first, for dashboards older than two trackers.
            "tracker": {"kind": tracker.primary.kind, "label": tracker.primary.label,
                        "enqueue": tracker.primary.enqueue_hint()},
            "trackers": [{"kind": member.kind, "label": member.label, "enqueue": member.enqueue_hint()}
                         for member in tracker.members],
            "plan_usage": plan_usage.read(),
            "jobs": [dict(job, files=self.files(job["id"])) for job in self.all()],
        }

    def write_index(self):
        cfg = self.cfg
        self.dir.mkdir(parents=True, exist_ok=True)
        write_atomic(self.dir / "index.json", json.dumps(self.index(), indent=1) + "\n")
        write_atomic(self.dir / "graph.json", json.dumps(graph.as_json(cfg), indent=1) + "\n")
        write_atomic(self.dir / "config.json", json.dumps(config.as_json(cfg), indent=1) + "\n")

    def reconcile_index(self) -> list[str]:
        """Rewrite index.json when its job ids differ from the folders on disk.

        Returns the ids that were listed and are gone. A missing index is left alone. A file that is not a
        job index is rewritten and reported as ``["<unreadable>"]``.
        """
        path: Path = self.dir / "index.json"
        live: set[str] = set(self.ids())
        if not path.is_file():
            return []
        listed: list[str] | None = self._index_ids(path)
        if listed is None:
            self.write_index()
            return ["<unreadable>"]
        if set(listed) == live:
            return []
        self.write_index()
        return [ident for ident in listed if ident not in live]

    def _index_ids(self, path: Path) -> list[str] | None:
        """Job ids in index.json, or None when the file is not a job index."""
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
        raw_jobs: object = loaded.get("jobs")
        if not isinstance(raw_jobs, list):
            return None
        listed: list[str] = []
        for item in raw_jobs:
            if not isinstance(item, dict):
                continue
            ident: object = item.get("id")
            if isinstance(ident, str):
                listed.append(ident)
        return listed
