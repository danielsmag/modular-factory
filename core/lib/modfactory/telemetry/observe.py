"""Turn persisted job transitions into telemetry events. Telemetry never decides the next step."""

import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..graph import END_STATES
from ..util import FactoryError, now, parse_time, write_atomic
from .artifacts import artifact_metadata
from .collector import Collector
from .errors import TelemetryError
from .fingerprint import Fingerprint, fingerprint_config, git_metrics, sanitize
from .schema import stable_id
from .settings import TelemetrySettings, settings_from
from .sink import LocalJsonlSink
from .usage import claude_snapshots, codex_unavailable, cursor_snapshots, plan_snapshots, share_snapshots

_BAD_OUTCOMES: frozenset[str] = frozenset({"FAIL", "BLOCKED", "FAILED", "REJECT"})


class Observer:
    """Fail-open adapter between ``Factory`` and the collector."""

    def __init__(self, settings: TelemetrySettings, sink: LocalJsonlSink | None) -> None:
        self._settings: TelemetrySettings = settings
        self._sink: LocalJsonlSink | None = sink
        self._collector: Collector | None = Collector(sink, settings) if sink is not None and settings.enabled else None
        self._open: set[str] = set()
        self._runs: dict[str, str] = {}
        self._hashes: dict[str, str] = {}

    @classmethod
    def for_config(cls, cfg: Any) -> "Observer":
        """A live collector, or a no-op when ``[telemetry].enabled`` is false."""
        settings: TelemetrySettings = settings_from(cfg)
        if not settings.enabled:
            return cls(settings, None)
        return cls(settings, LocalJsonlSink(Path(cfg.repo) / ".factory"))

    @contextmanager
    def session(self, factory: Any, job: dict[str, Any]) -> Iterator[None]:
        """One CLI invocation. A crash leaves the run open so the next command can see the gap."""
        self.begin_run(factory, job)
        try:
            yield
        finally:
            self.end_run(factory, job)

    def begin_run(self, factory: Any, job: dict[str, Any]) -> None:
        self._call(lambda: self._begin(factory, job))

    def end_run(self, factory: Any, job: dict[str, Any]) -> None:
        self._call(lambda: self._end(factory, job))

    def after_create(self, factory: Any, job: dict[str, Any], skipped: list[str]) -> None:
        self._call(lambda: self._after_create(factory, job, skipped))

    def after_transition(self, factory: Any, job: dict[str, Any], prior: dict[str, Any]) -> None:
        self._call(lambda: self._after_transition(factory, job, prior))

    def step_dispatched(self, factory: Any, job: dict[str, Any], agents: list[str]) -> None:
        self._call(lambda: self._dispatched(factory, job, agents))

    def check_completed(self, factory: Any, job: dict[str, Any], verdict: Any) -> None:
        self._call(lambda: self._check(factory, job, verdict))

    def _call(self, fn: Any) -> None:
        """Telemetry boundary. A failure here must not change the job unless fail-open is off."""
        if self._collector is None:
            return
        try:
            fn()
        except (TelemetryError, OSError, ValueError, TypeError, KeyError) as err:
            self._collector.note(err)

    def _begin(self, factory: Any, job: dict[str, Any]) -> None:
        store: LocalJsonlSink = self._require()
        job_id: str = str(job["id"])
        if job_id in self._open:
            return
        manifest: dict[str, Any] = store.store.manifest(job_id)
        runs: list[Any] = list(manifest.get("runs") or [])
        pid: int = os.getpid()
        for run in runs:
            if not isinstance(run, dict) or run.get("ended_at") is not None:
                continue
            old: object = run.get("run_id")
            if isinstance(old, str):
                self._interrupted(factory, job, old)
            run["ended_at"] = now()
        run_id: str = str(uuid.uuid4())
        self._runs[job_id] = run_id
        self._open.add(job_id)
        runs.append({
            "run_id": run_id, "pid": pid, "started_at": now(), "ended_at": None, "step": job.get("step"),
        })
        store.store.update_manifest(job_id, {"runs": runs})
        config_hash: str | None = self._ensure_config(factory, job)
        self._emit(
            "run.started", job, f"run.started|{job_id}|{run_id}",
            {"step": _text(job.get("step"))}, step=_text(job.get("step")), config_hash=config_hash,
        )
        self._reconcile(factory, job, config_hash)

    def _end(self, factory: Any, job: dict[str, Any]) -> None:
        store: LocalJsonlSink = self._require()
        job_id: str = str(job["id"])
        if job_id not in self._open:
            return
        run_id: str | None = self._runs.get(job_id)
        self._open.discard(job_id)
        manifest: dict[str, Any] = store.store.manifest(job_id)
        runs: list[Any] = list(manifest.get("runs") or [])
        for run in runs:
            if isinstance(run, dict) and run.get("run_id") == run_id and run.get("ended_at") is None:
                run["ended_at"] = now()
        store.store.update_manifest(job_id, {"runs": runs})
        self._emit(
            "run.ended", job, f"run.ended|{job_id}|{run_id}",
            {"step": _text(job.get("step")), "outcome": "session_end"},
            step=_text(job.get("step")), run_id=run_id,
        )

    def _after_create(self, factory: Any, job: dict[str, Any], skipped: list[str]) -> None:
        config_hash: str | None = self._ensure_config(factory, job)
        self._created(factory, job, config_hash)
        for name in skipped:
            self._skipped(factory, job, name, "create", config_hash)
        self._entered(factory, job, config_hash)
        self._human_requested(job, config_hash)
        self._usage(factory, job, config_hash)
        self._artifact(factory, job, config_hash)

    def _after_transition(self, factory: Any, job: dict[str, Any], prior: dict[str, Any]) -> None:
        history: object = job.get("history")
        if not isinstance(history, list) or not history:
            return
        index: int = len(history) - 1
        entry: object = history[index]
        if not isinstance(entry, dict):
            return
        config_hash: str | None = self._ensure_config(factory, job)
        self._history(factory, job, index, entry, prior, config_hash, live=True)
        self._mark(str(job["id"]), index)
        landed: str | None = _text(job.get("step"))
        if landed in END_STATES:
            self._finished(job, config_hash)
        else:
            self._entered(factory, job, config_hash)
            self._human_requested(job, config_hash)
        self._usage(factory, job, config_hash)
        self._artifact(factory, job, config_hash)

    def _reconcile(self, factory: Any, job: dict[str, Any], config_hash: str | None) -> None:
        store: LocalJsonlSink = self._require()
        manifest: dict[str, Any] = store.store.manifest(str(job["id"]))
        done: set[int] = {item for item in manifest.get("transitions") or [] if isinstance(item, int)}
        self._created(factory, job, config_hash)
        history: object = job.get("history")
        if isinstance(history, list):
            for index, entry in enumerate(history):
                if index in done or not isinstance(entry, dict):
                    continue
                prior: dict[str, Any] = {
                    "type": self._step_type(factory, job, _text(entry.get("step"))),
                    "round": None,
                    "attempt": None,
                    "reason_kind": None,
                    "reason_present": bool(entry.get("reason")),
                }
                self._history(factory, job, index, entry, prior, config_hash, live=False)
                self._mark(str(job["id"]), index)
        if _text(job.get("step")) in END_STATES:
            self._finished(job, config_hash)
        elif _text(job.get("entered_at")):
            self._entered(factory, job, config_hash)
        self._human_requested(job, config_hash)

    def _history(
        self, factory: Any, job: dict[str, Any], index: int, entry: dict[str, Any],
        prior: dict[str, Any], config_hash: str | None, *, live: bool,
    ) -> None:
        step: str | None = _text(entry.get("step"))
        outcome: str | None = _text(entry.get("outcome"))
        target: str | None = _text(entry.get("to"))
        entered: str | None = _text(entry.get("entered_at"))
        job_id: str = str(job["id"])
        skipped: list[str] = _names(entry.get("skipped"))
        reason_present: bool = bool(prior.get("reason_present")) or bool(entry.get("reason"))
        reason_kind: str | None = _text(prior.get("reason_kind")) or _reason_kind(prior.get("type"), outcome)
        attempt: int | None = _whole(prior.get("attempt"))
        round_number: int | None = _whole(prior.get("round"))
        common: dict[str, Any] = {
            "outcome": outcome,
            "to_step": target,
            "skipped": skipped,
            "reason_present": reason_present,
            "reason_kind": reason_kind,
        }
        completed: dict[str, Any] = dict(common, **_model(factory, job, step or ""))
        completed.update(_duration(entered, _text(entry.get("at"))))
        if self._settings.collect["git_metrics"]:
            completed.update(_git(factory, job))
        self._emit(
            "step.completed", job, f"step.completed|{job_id}|{index}|{step}|{outcome}|{entered}|{target}",
            completed, step=step, attempt=attempt, round_number=round_number, config_hash=config_hash,
            entered_at=entered,
        )
        self._emit(
            "step.transitioned", job, f"step.transitioned|{job_id}|{index}|{step}|{outcome}|{target}",
            {"from_step": step, **common}, step=step, attempt=attempt, round_number=round_number,
            config_hash=config_hash, entered_at=entered,
        )
        for name in skipped:
            self._skipped(factory, job, name, str(index), config_hash, entered)
        if outcome in _BAD_OUTCOMES or reason_kind == "limit":
            self._emit(
                "error.occurred", job, f"error.occurred|{job_id}|{index}|{step}|{outcome}",
                {
                    "category": _text(prior.get("type")) or "step",
                    "code": outcome,
                    "producer": "core",
                    "retryable": target not in END_STATES,
                },
                step=step, config_hash=config_hash, entered_at=entered, source="core",
            )
        if not live:
            return
        kind: object = prior.get("type")
        if kind == "review":
            self._reviews(job, step, index, entered, attempt, round_number, config_hash)
        elif kind == "human":
            self._emit(
                "human.responded", job, f"human.responded|{job_id}|{index}|{outcome}",
                {"outcome": outcome, "has_text": None}, step=step, config_hash=config_hash, entered_at=entered,
            )
        elif kind in {"agent", "verify"}:
            self._emit(
                "agent.completed", job, f"agent.completed|{job_id}|{index}|{step}",
                dict(_model(factory, job, step or ""), outcome=outcome, role=_role(factory, job, step)),
                step=step, attempt=attempt, round_number=round_number, config_hash=config_hash, entered_at=entered,
                source="adapter",
            )

    def _created(self, factory: Any, job: dict[str, Any], config_hash: str | None) -> None:
        self._emit(
            "job.created", job, f"job.created|{job['id']}",
            {
                "pipeline": _text(job.get("pipeline")),
                "issue_ref": _issue(factory, job),
                "tracker": _text(job.get("tracker")),
            },
            config_hash=config_hash,
        )

    def _entered(self, factory: Any, job: dict[str, Any], config_hash: str | None) -> None:
        step: str | None = _text(job.get("step"))
        if step is None or step in END_STATES:
            return
        entered: str | None = _text(job.get("entered_at"))
        payload: dict[str, Any] = dict(_model(factory, job, step), pipeline=_text(job.get("pipeline")))
        payload["role"] = _role(factory, job, step)
        self._emit(
            "step.entered", job, f"step.entered|{job['id']}|{step}|{entered}", payload,
            step=step, attempt=_whole(job.get("attempt")), round_number=_whole(job.get("round")),
            config_hash=config_hash, entered_at=entered,
        )

    def _finished(self, job: dict[str, Any], config_hash: str | None) -> None:
        pr: object = job.get("pr")
        self._emit(
            "job.completed", job, f"job.completed|{job['id']}|{_text(job.get('step'))}|{_text(job.get('finished_at'))}",
            {
                "outcome": _text(job.get("step")),
                "pr": pr if isinstance(pr, int) and not isinstance(pr, bool) else None,
                "merged": bool(job.get("merge_sha")),
            },
            step=_text(job.get("step")), config_hash=config_hash,
        )

    def _skipped(
        self, factory: Any, job: dict[str, Any], name: str, token: str, config_hash: str | None,
        entered: str | None = None,
    ) -> None:
        self._emit(
            "step.skipped", job, f"step.skipped|{job['id']}|{token}|{name}",
            {"skipped_step": name, "skip_outcome": _skip_outcome(factory, job, name), "reason_kind": "disabled"},
            step=name, config_hash=config_hash, entered_at=entered,
        )

    def _human_requested(self, job: dict[str, Any], config_hash: str | None) -> None:
        if not isinstance(job.get("waiting"), dict):
            return
        who_kind, who_count = _who(job)
        step: str | None = _text(job.get("step"))
        entered: str | None = _text(job.get("entered_at"))
        self._emit(
            "human.requested", job, f"human.requested|{job['id']}|{step}|{entered}",
            {"who_kind": who_kind, "who_count": who_count},
            step=step, config_hash=config_hash, entered_at=entered,
        )

    def _reviews(
        self, job: dict[str, Any], step: str | None, index: int, entered: str | None, attempt: int | None,
        round_number: int | None, config_hash: str | None,
    ) -> None:
        reviews: object = job.get("reviews")
        if not isinstance(reviews, dict):
            return
        for reviewer, verdict in reviews.items():
            if not isinstance(reviewer, str) or not isinstance(verdict, str):
                continue
            agent: str = stable_id(f"agent|{job['id']}|{index}|{reviewer}")
            self._emit(
                "review.completed", job, f"review.completed|{job['id']}|{index}|{reviewer}|{verdict}",
                {"reviewer": reviewer, "verdict": verdict},
                step=step, attempt=attempt, round_number=round_number, config_hash=config_hash,
                entered_at=entered, agent=agent, source="core",
            )

    def _interrupted(self, factory: Any, job: dict[str, Any], run_id: str) -> None:
        step: str | None = _text(job.get("step"))
        self._emit(
            "step.interrupted", job, f"step.interrupted|{job['id']}|{run_id}|{step}",
            {"reason_code": "session_not_closed", "open_run_id": run_id},
            step=step, run_id=run_id, config_hash=self._hashes.get(str(job["id"])),
        )

    def _dispatched(self, factory: Any, job: dict[str, Any], agents: list[str]) -> None:
        entered: str | None = _text(job.get("entered_at"))
        step: str | None = _text(job.get("step"))
        listed: list[dict[str, str]] = []
        for index, name in enumerate(agents):
            listed.append({
                "name": name,
                "agent_execution_id": stable_id(f"agent|{job['id']}|{step}|{entered}|{name}|{index}"),
            })
        run_id: str | None = self._runs.get(str(job["id"]))
        agent: str | None = listed[0]["agent_execution_id"] if len(listed) == 1 else None
        payload: dict[str, Any] = dict(
            _model(factory, job, step or ""), agent_count=len(listed), agents=listed, role=_role(factory, job, step),
        )
        self._emit(
            "step.dispatched", job, f"step.dispatched|{job['id']}|{run_id}|{step}|{entered}", payload,
            step=step, attempt=_whole(job.get("attempt")), round_number=_whole(job.get("round")),
            entered_at=entered, agent=agent, run_id=run_id,
        )

    def _check(self, factory: Any, job: dict[str, Any], verdict: Any) -> None:
        name: str = verdict.name if isinstance(getattr(verdict, "name", None), str) else "checks"
        outcome: str = str(verdict)
        exit_code: int | None = verdict.exit_code if isinstance(getattr(verdict, "exit_code", None), int) else None
        if isinstance(exit_code, bool):
            exit_code = None
        seconds: object = getattr(verdict, "seconds", None)
        duration: int | None = seconds * 1000 if isinstance(seconds, int) and not isinstance(seconds, bool) else None
        timed_out: object = getattr(verdict, "timed_out", None)
        commit: object = getattr(verdict, "commit", None)
        step: str | None = _text(job.get("step"))
        self._emit(
            "check.completed", job,
            f"check.completed|{job['id']}|{_whole(job.get('round'))}|{_whole(job.get('attempt'))}|{name}|{outcome}",
            {
                "name": name,
                "outcome": outcome,
                "exit_code": exit_code,
                "timed_out": timed_out if isinstance(timed_out, bool) else None,
                "commit": commit if isinstance(commit, str) else None,
                "duration_ms": duration,
                "duration_kind": "monotonic_process" if duration is not None else None,
                "duration_interval": "command",
            },
            step=step, attempt=_whole(job.get("attempt")), round_number=_whole(job.get("round")),
            entered_at=_text(job.get("entered_at")), source="check",
        )

    def _usage(self, factory: Any, job: dict[str, Any], config_hash: str | None) -> None:
        if not (self._settings.collect["usage"] or self._settings.collect["cost"]):
            return
        store: LocalJsonlSink = self._require()
        job_id: str = str(job["id"])
        manifest: dict[str, Any] = store.store.manifest(job_id)
        memory: dict[str, Any] = dict(manifest.get("usage") or {}) if isinstance(manifest.get("usage"), dict) else {}
        job_dir: Path = Path(factory.dir(job))
        events: list[tuple[str, dict[str, Any]]] = []
        batch: list[tuple[str, dict[str, Any]]]
        batch, memory = claude_snapshots(factory.cfg, job, job_dir, memory)
        events.extend(batch)
        batch, memory = cursor_snapshots(factory.cfg, job, job_dir, memory)
        events.extend(batch)
        events.extend(codex_unavailable(factory.cfg, job, memory))
        if self._settings.collect["usage"]:
            batch, memory = plan_snapshots(job, memory)
            events.extend(batch)
            batch, memory = share_snapshots(job, memory)
            events.extend(batch)
        step: str | None = _text(job.get("step"))
        for occurrence, payload in events:
            self._emit(
                "usage.recorded", job, occurrence, payload, source="adapter", step=step,
                attempt=_whole(job.get("attempt")), round_number=_whole(job.get("round")),
                config_hash=config_hash, entered_at=_text(job.get("entered_at")),
            )
        store.store.update_manifest(job_id, {"usage": memory})

    def _artifact(self, factory: Any, job: dict[str, Any], config_hash: str | None) -> None:
        if not self._settings.collect["artifact_metadata"]:
            return
        meta: dict[str, Any] | None = artifact_metadata(Path(factory.dir(job)), job.get("last_file"))
        if meta is None:
            return
        self._emit(
            "artifact.recorded", job, f"artifact.recorded|{job['id']}|{meta['path']}|{meta['sha256']}", meta,
            step=_text(job.get("step")), config_hash=config_hash, source="core",
        )

    def _ensure_config(self, factory: Any, job: dict[str, Any]) -> str | None:
        job_id: str = str(job["id"])
        if job_id in self._hashes:
            return self._hashes[job_id]
        if not self._settings.collect["config_fingerprints"]:
            return None
        finger: Fingerprint = fingerprint_config(factory.cfg, job)
        self._hashes[job_id] = finger.factory_config_hash
        store: LocalJsonlSink = self._require()
        manifest: dict[str, Any] = store.store.manifest(job_id)
        if manifest.get("config_hash") == finger.factory_config_hash:
            return finger.factory_config_hash
        stored: bool = self._snapshot(factory, finger)
        self._emit(
            "config.snapshot", job, f"config.snapshot|{job_id}|{finger.factory_config_hash}",
            finger.as_payload(snapshot_stored=stored), config_hash=finger.factory_config_hash,
        )
        store.store.update_manifest(job_id, {"config_hash": finger.factory_config_hash})
        return finger.factory_config_hash

    def _snapshot(self, factory: Any, finger: Fingerprint) -> bool:
        if not self._settings.privacy.get("store_config_content"):
            return False
        data: dict[str, Any] = dict(factory.cfg.data)
        data.pop("telemetry", None)
        path: Path = self._require().store.snapshot_path(finger.factory_config_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(sanitize(data), indent=2, sort_keys=True) + "\n")
        return True

    def _mark(self, job_id: str, index: int) -> None:
        store: LocalJsonlSink = self._require()
        manifest: dict[str, Any] = store.store.manifest(job_id)
        found: list[int] = [item for item in manifest.get("transitions") or [] if isinstance(item, int)]
        if index not in found:
            found.append(index)
        store.store.update_manifest(job_id, {"transitions": found})

    def _emit(
        self,
        event_type: str,
        job: dict[str, Any],
        occurrence: str,
        payload: dict[str, Any],
        *,
        source: str = "core",
        step: str | None = None,
        attempt: int | None = None,
        round_number: int | None = None,
        agent: str | None = None,
        config_hash: str | None = None,
        entered_at: str | None = None,
        run_id: str | None = None,
    ) -> None:
        collector: Collector = self._require_collector()
        job_id: str = str(job["id"])
        stamp: str = entered_at if entered_at is not None else (_text(job.get("entered_at")) or "")
        step_exec: str | None = stable_id(f"step-exec|{job_id}|{step}|{stamp}") if step else None
        collector.emit(
            event_type,
            occurrence_key=occurrence,
            payload=payload,
            source=source,
            job_id=job_id,
            run_id=self._runs.get(job_id) if run_id is None else run_id,
            step_execution_id=step_exec,
            agent_execution_id=agent,
            step=step,
            attempt=attempt,
            round=round_number,
            config_hash=self._hashes.get(job_id) if config_hash is None else config_hash,
        )

    def _require(self) -> LocalJsonlSink:
        if self._sink is None:
            raise TelemetryError("telemetry is disabled")
        return self._sink

    def _require_collector(self) -> Collector:
        if self._collector is None:
            raise TelemetryError("telemetry is disabled")
        return self._collector

    def _step_type(self, factory: Any, job: dict[str, Any], step: str | None) -> str | None:
        if step is None:
            return None
        try:
            found: object = factory.cfg.pipeline_of(job).steps.get(step)
        except (KeyError, TypeError, ValueError, FactoryError):
            return None
        if isinstance(found, dict) and isinstance(found.get("type"), str):
            return str(found["type"])
        return None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _whole(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _duration(start: str | None, end: str | None) -> dict[str, Any]:
    if start is None or end is None:
        return {"duration_ms": None, "duration_kind": None, "duration_interval": None}
    try:
        millis: int = int((parse_time(end) - parse_time(start)).total_seconds() * 1000)
    except (TypeError, ValueError):
        return {"duration_ms": None, "duration_kind": None, "duration_interval": None}
    if millis < 0:
        return {"duration_ms": None, "duration_kind": None, "duration_interval": None}
    return {"duration_ms": millis, "duration_kind": "wall_clock", "duration_interval": "dispatch_to_completion"}


def _reason_kind(step_type: object, outcome: str | None) -> str | None:
    if outcome in {"MOVED-BY-HAND", "CLOSED-BY-HAND"}:
        return "hand"
    if step_type == "human":
        return "human"
    if step_type == "checks":
        return "gate"
    if step_type == "review":
        return "review"
    return None


def _model(factory: Any, job: dict[str, Any], step: str) -> dict[str, Any]:
    tools: list[Any] = list(getattr(factory.cfg, "tools", []) or [])
    tool: str | None = tools[0] if tools and isinstance(tools[0], str) else None
    intake: object = job.get("intake")
    risk: str | None = _text(intake.get("risk")) if isinstance(intake, dict) else None
    chosen: dict[str, Any] = {}
    if tool is not None and step:
        try:
            found: object = factory.cfg.pipeline_of(job).model_for(tool, step, risk)
        except (KeyError, TypeError, ValueError, FactoryError):
            found = {}
        if isinstance(found, dict):
            chosen = found
    return {
        "tool": tool,
        "configured_model": chosen.get("model") if isinstance(chosen.get("model"), str) else None,
        "resolved_model": None,
        "effort": chosen.get("effort") if isinstance(chosen.get("effort"), str) else None,
        "risk": risk,
    }


def _role(factory: Any, job: dict[str, Any], step: str | None) -> str | None:
    if step is None:
        return None
    try:
        pipe: Any = factory.cfg.pipeline_of(job)
        found: object = pipe.steps.get(step)
    except (KeyError, TypeError, ValueError, FactoryError):
        return None
    if not isinstance(found, dict):
        return None
    if found.get("type") == "agent":
        role: object = pipe.role(step)
        return role if isinstance(role, str) else None
    kind: object = found.get("type")
    return kind if isinstance(kind, str) else None


def _skip_outcome(factory: Any, job: dict[str, Any], step: str) -> str | None:
    try:
        found: object = factory.cfg.pipeline_of(job).steps.get(step)
    except (KeyError, TypeError, ValueError, FactoryError):
        return None
    if isinstance(found, dict) and isinstance(found.get("skip"), str):
        return str(found["skip"])
    return None


def _issue(factory: Any, job: dict[str, Any]) -> str | None:
    try:
        return str(factory.tracker.ref(job["issue"]))
    except (FactoryError, KeyError, TypeError, ValueError):
        return None


def _who(job: dict[str, Any]) -> tuple[str, int | None]:
    waiting: object = job.get("waiting")
    who: object = waiting.get("who") if isinstance(waiting, dict) else None
    if isinstance(who, list):
        return "named", len(who)
    if who in {"author", "operator", "maintainer"}:
        return str(who), None
    if isinstance(who, str):
        return "role", None
    return "unknown", None


def _git(factory: Any, job: dict[str, Any]) -> dict[str, Any]:
    try:
        metrics: dict[str, Any] = git_metrics(
            Path(factory.cfg.repo), job.get("worktree") if isinstance(job.get("worktree"), str) else None,
            str(factory.cfg.base),
        )
    except (OSError, TypeError, ValueError):
        return {}
    pr: object = job.get("pr")
    metrics["pr"] = pr if isinstance(pr, int) and not isinstance(pr, bool) else None
    metrics["merged"] = bool(job.get("merge_sha"))
    return metrics
