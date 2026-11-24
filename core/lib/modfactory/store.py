"""Job state on disk: `.factory/jobs/<id>/job.json` and the dashboard's index."""

import fcntl
import json
import re

from . import config, graph
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
        """A job id (`004-dark-mode`), its number as in the id (`004`), or its issue (`12` or `#12`)."""
        ref = str(ref)
        jobs = self.all()
        matches = [j for j in jobs if j["id"] == ref or j["id"].startswith(ref + "-")]
        if not matches and ref.lstrip("#").isdigit():
            matches = [j for j in jobs if j["issue"] == int(ref.lstrip("#"))]
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
        numbers = [int(i.split("-")[0]) for i in self.ids()]
        words = re.findall(r"[a-z0-9]+", title.lower())[:4]
        number = max(numbers, default=0) + 1
        return number, f"{number:03d}-{'-'.join(words) or 'issue'}"

    def save(self, job):
        job["schema"] = SCHEMA
        job["updated_at"] = now()
        self.path(job["id"]).mkdir(parents=True, exist_ok=True)
        write_atomic(self.path(job["id"]) / "job.json", json.dumps(job, indent=2) + "\n")
        self.write_index()

    # -- slots

    def holds_slot(self, job):
        step = self.cfg.steps.get(job["step"])
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
        return {
            "schema": SCHEMA,
            "generated_at": now(),
            "version": version(),
            "project": cfg["project"],
            "repo": cfg.repo.name,
            "tools": cfg.tools,
            "concurrency": cfg["run"]["concurrency"],
            "labels": cfg["labels"],
            "jobs": [dict(job, files=self.files(job["id"])) for job in self.all()],
        }

    def write_index(self):
        cfg = self.cfg
        self.dir.mkdir(parents=True, exist_ok=True)
        write_atomic(self.dir / "index.json", json.dumps(self.index(), indent=1) + "\n")
        write_atomic(self.dir / "graph.json", json.dumps(graph.as_json(cfg), indent=1) + "\n")
        write_atomic(self.dir / "config.json", json.dumps(config.as_json(cfg), indent=1) + "\n")
