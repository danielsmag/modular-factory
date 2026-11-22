"""What to run: new jobs and the queue of labeled issues."""

from typing import Any, Protocol

from . import graph
from .config import Config
from .service import GitHub
from .service.human import audience
from .store import Store
from .util import Refused, now


class _QueueHost(Protocol):
    """Methods `Factory` supplies when it mixes `Queue` in."""

    cfg: Config
    gh: GitHub
    store: Store

    def _write_issue_md(self, job: dict[str, Any], issue: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def _enter(self, job: dict[str, Any], target: str) -> tuple[str, list[str]]: ...

    def _adopt_merge(self, job: dict[str, Any]) -> bool: ...

    def _find_reply(self, job: dict[str, Any], step: dict[str, Any]) -> tuple[str, str, str, Any] | None: ...


class Queue:
    """Job queue. `Factory` owns `cfg`, `gh`, and `store` and mixes this in."""

    cfg: Config
    gh: GitHub
    store: Store

    def create(self: _QueueHost, number: object) -> dict[str, Any]:
        """A new job for an open issue that carries the queue label."""
        number = int(str(number).lstrip("#"))
        labels = self.cfg["labels"]
        for j in self.store.all():
            if j["issue"] == number:
                raise Refused(f"issue #{number} already has job {j['id']} (at {j['step']}). "
                              f"To start over, delete .factory/jobs/{j['id']}.")
        running = [j["id"] for j in self.store.running()]
        limit = self.cfg["run"]["concurrency"]
        if len(running) >= limit:
            raise Refused(f"concurrency {limit} reached; running: {', '.join(running)}")
        issue = self.gh.issue(number)
        if issue["state"] != "OPEN":
            raise Refused(f"issue #{number} is {issue['state'].lower()}")
        if labels["queue"] not in {label["name"] for label in issue["labels"]}:
            raise Refused(f"issue #{number} isn't labeled `{labels['queue']}`. Only labeled issues run.")
        n, job_id = self.store.new_id(issue["title"])
        job = {
            "id": job_id, "number": n, "issue": number, "title": issue["title"],
            "issue_url": issue["url"], "author": issue["author"]["login"],
            "step": None, "entered_at": None, "running_since": None, "running_order": None,
            "round": 0, "attempt": 0, "gate_failed": False, "taken": {}, "handoff": [],
            "branch": f"factory/{job_id}",
            "worktree": str(self.cfg.repo / self.cfg["paths"]["worktrees"] / job_id),
            "pr": None, "pr_url": None, "merge_sha": None,
            "intake": None, "agents": None, "gate": None, "reviews": {},
            "waiting": None, "consumed": [], "reason": None, "escalated_from": None, "last_file": None,
            "entry_files": {}, "verify": None, "history": [], "cost_usd": None, "cost_by_step": {},
            "created_at": now(), "finished_at": None,
        }
        self.store.path(job_id).mkdir(parents=True)
        self.store.save(job)
        job = self.store.open(job_id)
        self._write_issue_md(job, issue)
        self._enter(job, self.cfg["run"]["start"])
        self.store.save(job)
        return job

    def queue(self: _QueueHost) -> list[tuple[Any, ...]]:
        """Rows of `action issue job step note`, in the order to act on them.

        `new` and `resume` rows fit in the concurrency slots. `hold` rows wait
        for a slot, `wait` rows for a person, and `skip` rows need nothing.
        """
        labels, limit = self.cfg["labels"], self.cfg["run"]["concurrency"]
        for stale in self.store.all():
            if stale["pr"] and not stale["merge_sha"] and stale["step"] not in graph.END_STATES:
                job = self.store.try_open(stale["id"])
                if job:
                    self._adopt_merge(job)
        jobs = {j["issue"]: j for j in self.store.all()}
        issues = self.gh.queue(labels["queue"])
        open_numbers = {i["number"] for i in issues}

        running = [j["id"] for j in self.store.running()]
        rows, replied, waiting, other = [], [], [], []
        for j in sorted(jobs.values(), key=lambda j: j["id"]):
            n, name = j["issue"], j["step"]
            step = self.cfg.steps.get(name)
            if name in graph.END_STATES:
                if n in open_numbers:
                    other.append(("skip", n, j["id"], name,
                                  f"finished; delete .factory/jobs/{j['id']} to run it again"))
            elif step is None:
                other.append(("skip", n, j["id"], name, "factory.toml no longer has this step"))
            elif step["type"] == "human":
                if self._find_reply(j, step):
                    replied.append(("resume", n, j["id"], name, "a reply is waiting"))
                else:
                    who_value: str | list[str] = step.get("who", "maintainer")
                    operator_login: str = self.gh.operator() if who_value == "operator" else ""
                    who_phrase: str = audience(who_value, j["author"], operator_login)
                    waiting.append(("wait", n, j["id"], name, f"waiting for {who_phrase}"))
            elif step["type"] == "verify":
                rows.append(("resume", n, j["id"], name, "validating after " + step["after"]))
            elif n not in open_numbers and not j["pr"]:
                other.append(("skip", n, j["id"], name, f"issue is closed or no longer labeled {labels['queue']}"))
            else:
                turn = running.index(j["id"]) < limit
                rows.append(("resume" if turn else "hold", n, j["id"], name,
                             f"round {j['round']}" if turn else f"concurrency {limit} reached"))
        free = limit - len(running)
        fresh = [("new", i["number"], "-", "-", i["title"])
                 for i in sorted(issues, key=lambda i: i["number"]) if i["number"] not in jobs]
        for row in replied + fresh:
            if free > 0:
                rows.append(row)
                free -= 1
            else:
                rows.append(("hold",) + row[1:4] + (f"concurrency {limit} reached",))
        return rows + waiting + other
