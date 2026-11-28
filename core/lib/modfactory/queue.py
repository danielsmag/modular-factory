"""What to run: new jobs and the tracker's queue of issues."""

from collections.abc import Callable
from typing import Any, Protocol

from . import config, graph
from .config import Config
from .planner import holds
from .service import GitHub
from .service.tracker import Key, Tracker, Trackers
from .service.tracker.base import LABELS, key_kind
from .store import Store
from .util import FactoryError, Refused, now


class _QueueHost(Protocol):
    """Methods `Factory` supplies when it mixes `Queue` in."""

    cfg: Config
    gh: GitHub
    tracker: Tracker | Trackers
    store: Store

    def _write_issue_md(self, job: dict[str, Any], issue: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def _enter(self, job: dict[str, Any], target: str) -> tuple[str, list[str]]: ...

    def _pipe(self, job: dict[str, Any] | None) -> Config: ...

    def _adopt_merge(self, job: dict[str, Any]) -> bool: ...

    def _find_reply(self, job: dict[str, Any], step: dict[str, Any]) -> tuple[str, str, str, Any] | None: ...

    def _audience(self, job: dict[str, Any], who: str | list[str]) -> str: ...

    def _who_of(self, job: dict[str, Any], step: dict[str, Any]) -> str | list[str]: ...

    def _settle_cards(self) -> set[str]: ...

    def queue_and_issues(self) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]]]: ...

    def _picks(self) -> Callable[[dict[str, Any]], bool]: ...

    def _reply_note(self, job: dict[str, Any], step: dict[str, Any], tracker: Tracker,
                    answered: bool) -> str: ...


class NeedsPipeline(Refused):
    """A new job could run more than one pipeline and none was chosen: the operator must pick one."""

    def __init__(self, cfg: Config):
        base: Config = cfg.base_config
        self.choices: list[dict[str, str]] = [
            {"name": name, "description": pipe.description, "file": pipe.file}
            for name, pipe in base.pipelines.items()]
        listed: str = "; ".join(f"{c['name']}" + (f" ({c['description']})" if c["description"] else "")
                                for c in self.choices)
        super().__init__(f"choose a pipe with --pipe NAME, or set [run].pipe in factory/factory.toml. "
                         f"Pipes: {listed}")


class Queue:
    """Job queue. `Factory` owns `cfg`, `gh`, `tracker`, and `store` and mixes this in."""

    cfg: Config
    gh: GitHub
    tracker: Tracker | Trackers
    store: Store

    def create(self: _QueueHost, number: object, pipeline: str | None = None) -> dict[str, Any]:
        """A new job for an open issue that is in the tracker's queue, running `pipeline`.

        Without `pipeline` the job runs [run].pipeline, or the project's only pipeline.
        With several and none chosen, it refuses with `NeedsPipeline`, which lists them.
        """
        key: Key = self.tracker.key(number)
        ref: str = self.tracker.ref(key)
        for j in self.store.all():
            if j["issue"] == key:
                raise Refused(f"issue {ref} already has job {j['id']} (at {j['step']}). "
                              f"To start over, run mfactory delete {j['id']}.")
        chosen: str | None = self.cfg.choose_pipeline(pipeline)
        if chosen is None:
            raise NeedsPipeline(self.cfg)
        running = [j["id"] for j in self.store.running()]
        limit = self.cfg["run"]["concurrency"]
        if len(running) >= limit:
            raise Refused(f"concurrency {limit} reached; running: {', '.join(running)}")
        issue = self.tracker.issue(key)
        if issue["state"] != "OPEN":
            raise Refused(f"issue {ref} is {issue['state'].lower()}")
        gate: str | None = self.tracker.queue_gate(issue)
        if gate:
            raise Refused(gate)
        n, job_id = self.store.new_id(issue["title"])
        branch: str = self.cfg.job_branch(job_id, tag=self.tracker.branch_tag(key))
        # A tracker whose logins are ids, not names, also gives the name people know the author by.
        named: dict[str, Any] = {"author_name": issue["author"]["name"]} if issue["author"].get("name") else {}
        job = {
            "id": job_id, "number": n, "issue": key, "tracker": self.tracker.of(key).kind, "title": issue["title"],
            "issue_url": issue["url"], "author": issue["author"]["login"], **named,
            "pipeline": chosen,
            "step": None, "entered_at": None, "running_since": None, "running_order": None,
            "round": 0, "attempt": 0, "gate_failed": False, "taken": {}, "handoff": [],
            "branch": branch,
            "worktree": str(self.cfg.repo / self.cfg["paths"]["worktrees"] / job_id),
            "pr": None, "pr_url": None, "merge_sha": None, "prs": [], "follow_ups": 0,
            "intake": None, "agents": None, "gate": None, "reviews": {},
            "waiting": None, "consumed": [], "reason": None, "escalated_from": None, "last_file": None,
            "entry_files": {}, "verify": None, "history": [], "cost_usd": None, "cost_by_step": {},
            "plan_by_step": {}, "usage_share_by_step": {},
            "created_at": now(), "finished_at": None,
        }
        self.store.path(job_id).mkdir(parents=True)
        job["held_by"] = holds.take_fresh(self, key)  # a hold noted before the job existed moves onto it
        if job["held_by"] is None:
            del job["held_by"]
        self.store.save(job)
        job = self.store.open(job_id)
        self._write_issue_md(job, issue)
        self._enter(job, self._pipe(job)["run"]["start"])
        self.store.save(job)
        return job

    def _picks(self: _QueueHost) -> Callable[[dict[str, Any]], bool]:
        """Which queued issues `queue` offers as new jobs: `[run].pick`, `authors` and `assignees`.

        An issue passes when its author or any assignee is named for its own tracker. A tracker with
        nobody named lets every issue through. `@me` is the account running the factory on that tracker.
        """
        run: dict[str, Any] = self.cfg["run"]
        mine: list[str] = ["@me"] if run.get("pick") == "mine" else []
        rules: dict[str, tuple[set[str], set[str]] | None] = {}

        def rule(tracker: Tracker) -> tuple[set[str], set[str]] | None:
            if tracker.kind not in rules:
                authors: list[str] = mine + config.people_for(run.get("authors"), tracker.kind)
                assignees: list[str] = mine + config.people_for(run.get("assignees"), tracker.kind)
                operator: str = ""
                if "@me" in authors + assignees:
                    try:
                        operator = tracker.operator()
                    except FactoryError as error:
                        self.tracker.failed(tracker, "@me", error)  # `@me` then names nobody on that tracker

                def logins(names: list[str]) -> set[str]:
                    return {operator if name == "@me" else name for name in names}
                rules[tracker.kind] = (logins(authors), logins(assignees)) if authors or assignees else None
            return rules[tracker.kind]

        def picks(issue: dict[str, Any]) -> bool:
            named: tuple[set[str], set[str]] | None = rule(self.tracker.of(issue["number"]))
            if named is None:
                return True
            by, to = named
            assigned: set[str] = {a["login"] for a in issue.get("assignees") or []}
            return issue["author"]["login"] in by or bool(assigned & to)
        return picks

    def _reply_note(self: _QueueHost, job: dict[str, Any], step: dict[str, Any], tracker: Tracker,
                    answered: bool) -> str:
        """`a reply is waiting`, who a parked job waits for, or why its tracker could not say."""
        if answered:
            return "a reply is waiting"
        try:
            if self._find_reply(job, step):
                return "a reply is waiting"
            return f"waiting for {self._audience(job, self._who_of(job, step))}"
        except FactoryError as error:
            # This job's own call failed; the other jobs on its tracker are read as usual.
            return f"could not read {tracker.label}: {self.tracker.failed(tracker, job['issue'], error)}"

    def queue(self: _QueueHost) -> list[tuple[Any, ...]]:
        """Rows of `action issue job step note`, in the order to act on them.

        `new` and `resume` rows fit in the concurrency slots. `hold` rows wait
        for a slot, `wait` rows for a person, and `skip` rows need nothing.
        """
        return self.queue_and_issues()[0]

    def queue_and_issues(self: _QueueHost) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]]]:
        """The rows of `queue`, and the open issues the tracker's queue holds."""
        limit = self.cfg["run"]["concurrency"]
        self.store.reconcile_index()
        for stale in self.store.all():
            if stale["pr"] and not stale["merge_sha"] and stale["step"] not in graph.END_STATES:
                job = self.store.try_open(stale["id"])
                if job:
                    self._adopt_merge(job)
        answered: set[str] = self._settle_cards()
        issues = self.tracker.queue()
        open_numbers = {i["number"] for i in issues}
        holds.lift(self, {str(n) for n in open_numbers})
        jobs = {j["issue"]: j for j in self.store.all()}
        fresh_held: dict[str, dict[str, Any]] = holds.fresh_holds(self)

        running = [j["id"] for j in self.store.running()]
        rows, replied, waiting, other = [], [], [], []
        for j in sorted(jobs.values(), key=lambda j: j["id"]):
            n, name = j["issue"], j["step"]
            step = self._pipe(j).steps.get(name)
            try:
                tracker: Tracker = self.tracker.of(n)
            except Refused:
                owner: str | None = key_kind(n)
                other.append(("skip", n, j["id"], name,
                              f"its tracker, {LABELS.get(owner or '', owner)}, is not in [tracker].kind"))
                continue
            down: bool = tracker.kind in self.tracker.unread
            if name in graph.END_STATES:
                if n in open_numbers:
                    other.append(("skip", n, j["id"], name,
                                  f"finished; mfactory delete {j['id']} to run it again"))
            elif step is None:
                other.append(("skip", n, j["id"], name, "factory.toml no longer has this step"))
            elif j.get("held_by"):
                waiting.append(("wait", n, j["id"], name, f"held until {j['held_by'].get('until')} is done"))
            elif step["type"] == "human":
                note: str = (f"could not read {tracker.label}: {self.tracker.reason(tracker.kind)}" if down
                             else self._reply_note(j, step, tracker, j["id"] in answered))
                if note == "a reply is waiting":
                    replied.append(("resume", n, j["id"], name, note))
                else:
                    waiting.append(("wait", n, j["id"], name, note))
            elif step["type"] == "verify":
                rows.append(("resume", n, j["id"], name, "validating after " + step["after"]))
            elif not down and not (j["pr"] or j.get("prs")) and (gone := self.tracker.withdrawn(n, open_numbers)):
                other.append(("skip", n, j["id"], name, gone))
            else:
                turn = running.index(j["id"]) < limit
                rows.append(("resume" if turn else "hold", n, j["id"], name,
                             f"round {j['round']}" if turn else f"concurrency {limit} reached"))
        free = limit - len(running)
        picks: Callable[[dict[str, Any]], bool] = self._picks()
        fresh = []
        for i in issues:
            if i["number"] in jobs or not picks(i):
                continue
            if (held := fresh_held.get(str(i["number"]))) is not None:
                waiting.append(("wait", i["number"], "-", "-", f"held until {held.get('until')} is done"))
            else:
                fresh.append(("new", i["number"], "-", "-", i["title"]))
        for row in replied + fresh:
            if free > 0:
                rows.append(row)
                free -= 1
            else:
                rows.append(("hold",) + row[1:4] + (f"concurrency {limit} reached",))
        return rows + waiting + other, issues

    def watch_rows(self: _QueueHost) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]]]:
        """Rows for the tracker's watched issues, and those issues. Only `queue --json` shows them, and no
        job is ever made from them: `queue` and `create` never see them."""
        jobs: set[Any] = {j["issue"] for j in self.store.all()}
        issues: list[dict[str, Any]] = [i for i in self.tracker.watched() if i["number"] not in jobs]
        return [("watch", i["number"], "-", "-", i.get("status") or "watched") for i in issues], issues


def queue_json(rows: list[tuple[Any, ...]], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`mfactory queue --json`: each row, plus what `gh issue list` said about its issue."""
    by_number: dict[int, dict[str, Any]] = {issue["number"]: issue for issue in issues}
    out: list[dict[str, Any]] = []
    for action, number, job, step, note in rows:
        issue: dict[str, Any] = by_number.get(number, {})
        author: object = issue.get("author")
        out.append({
            "action": action, "issue": number, "job": job, "step": step, "note": note,
            "title": issue.get("title"), "issue_url": issue.get("url"),
            "created_at": issue.get("createdAt"), "updated_at": issue.get("updatedAt"),
            "author": (author.get("name") or author.get("login")) if isinstance(author, dict) else None,
            "labels": [label["name"] for label in issue.get("labels") or []],
        })
    return out
