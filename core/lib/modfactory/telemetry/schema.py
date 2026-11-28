"""Versioned telemetry envelope. Readers keep unknown fields and report unknown versions."""

import uuid
from typing import Any

from .errors import TelemetryError

SCHEMA_VERSION: int = 1
NAMESPACE: uuid.UUID = uuid.UUID("6f0e2c1a-9b4d-4e77-8c3a-1d5b7a9e0f42")
EVENT_TYPES: frozenset[str] = frozenset({
    "job.created",
    "job.completed",
    "run.started",
    "run.ended",
    "config.snapshot",
    "step.entered",
    "step.dispatched",
    "step.completed",
    "step.transitioned",
    "step.skipped",
    "step.interrupted",
    "agent.started",
    "agent.completed",
    "check.completed",
    "review.completed",
    "human.requested",
    "human.responded",
    "usage.recorded",
    "artifact.recorded",
    "error.occurred",
})
_STEP_EVENTS: frozenset[str] = frozenset({
    "step.entered", "step.dispatched", "step.completed", "step.transitioned",
    "step.skipped", "step.interrupted", "agent.started", "agent.completed",
    "check.completed", "review.completed",
})
_ENVELOPE: tuple[str, ...] = (
    "schema_version", "event_id", "event_type", "timestamp", "source", "project_id",
    "job_id", "run_id", "step_execution_id", "agent_execution_id", "step", "attempt",
    "round", "factory_version", "config_hash", "occurrence_key", "payload",
)
_MODEL: frozenset[str] = frozenset({
    "tool", "configured_model", "resolved_model", "models", "effort", "risk", "role",
})
_TIME: frozenset[str] = frozenset({"duration_ms", "duration_kind", "duration_interval"})
_GIT: frozenset[str] = frozenset({
    "base_commit", "head_commit", "files_changed", "insertions", "deletions", "pr", "merged",
})
PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "job.created": frozenset({"pipeline", "issue_ref", "tracker"}),
    "job.completed": frozenset({"outcome", "pr", "merged"}),
    "run.started": frozenset({"step"}),
    "run.ended": frozenset({"step", "outcome"}),
    "config.snapshot": frozenset({
        "factory_version", "factory_config_hash", "pipeline_hash", "graph_hash", "base_commit",
        "roles", "skills_hash", "rules_hash", "adapter", "adapters", "adapter_version",
        "snapshot_stored",
    }),
    "step.entered": _MODEL | frozenset({"pipeline"}),
    "step.dispatched": _MODEL | frozenset({"agent_count", "agents"}),
    "step.completed": _MODEL | _TIME | _GIT | frozenset({
        "outcome", "to_step", "skipped", "reason_present", "reason_kind",
    }),
    "step.transitioned": frozenset({
        "outcome", "from_step", "to_step", "skipped", "reason_present", "reason_kind",
    }),
    "step.skipped": frozenset({"skipped_step", "skip_outcome", "reason_kind"}),
    "step.interrupted": frozenset({"reason_code", "open_run_id"}),
    "agent.started": _MODEL | _TIME | frozenset({"outcome"}),
    "agent.completed": _MODEL | _TIME | frozenset({"outcome"}),
    "check.completed": _TIME | frozenset({"name", "outcome", "exit_code", "timed_out", "commit"}),
    "review.completed": frozenset({"reviewer", "verdict"}),
    "human.requested": frozenset({"who_kind", "who_count"}),
    "human.responded": frozenset({"outcome", "has_text"}),
    "usage.recorded": frozenset({
        "provider", "source", "cost_kind", "cost_usd", "delta_cost_usd", "currency",
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
        "delta_input_tokens", "delta_output_tokens", "unpriced_models", "models",
        "price_table_version", "aggregation", "attribution_key", "plan_points", "usage_share_percent",
    }),
    "artifact.recorded": frozenset({"path", "artifact_type", "size_bytes", "sha256"}),
    "error.occurred": frozenset({"category", "code", "producer", "retryable"}),
}
CSV_ENVELOPE: tuple[str, ...] = _ENVELOPE[:-1]


def stable_id(occurrence_key: str) -> str:
    """The same logical occurrence always gets the same event id."""
    return str(uuid.uuid5(NAMESPACE, occurrence_key))


class Event:
    """One telemetry record. ``extra`` keeps fields this version does not know."""

    def __init__(
        self,
        *,
        schema_version: int,
        event_id: str,
        event_type: str,
        timestamp: str,
        source: str,
        project_id: str,
        occurrence_key: str,
        payload: dict[str, Any],
        job_id: str | None = None,
        run_id: str | None = None,
        step_execution_id: str | None = None,
        agent_execution_id: str | None = None,
        step: str | None = None,
        attempt: int | None = None,
        round: int | None = None,
        factory_version: str | None = None,
        config_hash: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.schema_version: int = schema_version
        self.event_id: str = event_id
        self.event_type: str = event_type
        self.timestamp: str = timestamp
        self.source: str = source
        self.project_id: str = project_id
        self.occurrence_key: str = occurrence_key
        self.payload: dict[str, Any] = payload
        self.job_id: str | None = job_id
        self.run_id: str | None = run_id
        self.step_execution_id: str | None = step_execution_id
        self.agent_execution_id: str | None = agent_execution_id
        self.step: str | None = step
        self.attempt: int | None = attempt
        self.round: int | None = round
        self.factory_version: str | None = factory_version
        self.config_hash: str | None = config_hash
        self.extra: dict[str, Any] = dict(extra or {})

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "source": self.source,
            "project_id": self.project_id,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "step_execution_id": self.step_execution_id,
            "agent_execution_id": self.agent_execution_id,
            "step": self.step,
            "attempt": self.attempt,
            "round": self.round,
            "factory_version": self.factory_version,
            "config_hash": self.config_hash,
            "occurrence_key": self.occurrence_key,
            "payload": self.payload,
        }
        body.update(self.extra)
        return body


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise TelemetryError("expected a string or null")


def _whole(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TelemetryError("expected a whole number or null")
    return value


def schema_problems(raw: object) -> list[str]:
    """What is wrong with one decoded record. Empty means it can be read."""
    if not isinstance(raw, dict):
        return ["event is not an object"]
    problems: list[str] = []
    version: object = raw.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        problems.append("schema_version is missing")
    elif version != SCHEMA_VERSION:
        problems.append(f"unsupported schema_version {version}")
    for key in ("event_id", "event_type", "timestamp", "source", "project_id", "occurrence_key"):
        if not isinstance(raw.get(key), str) or not str(raw.get(key)).strip():
            problems.append(f"{key} is missing")
    if not isinstance(raw.get("payload"), dict):
        problems.append("payload is missing")
    event_type: object = raw.get("event_type")
    if isinstance(event_type, str) and event_type not in EVENT_TYPES:
        problems.append(f"unknown event_type {event_type}")
    if isinstance(event_type, str) and event_type in _STEP_EVENTS:
        if not isinstance(raw.get("job_id"), str):
            problems.append(f"{event_type} is missing job_id")
        if event_type != "step.interrupted" and not isinstance(raw.get("step"), str):
            problems.append(f"{event_type} is missing step")
        if event_type in {"step.entered", "step.completed", "step.transitioned"} and not raw.get("config_hash"):
            problems.append(f"{event_type} is missing config_hash")
    for key in ("attempt", "round"):
        value: object = raw.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            problems.append(f"{key} must be a whole number or null")
    return problems


def event_from_dict(raw: object) -> Event:
    """Parse one record. Unknown fields are kept. A bad envelope raises."""
    problems: list[str] = schema_problems(raw)
    blocking: list[str] = [
        item for item in problems
        if item == "event is not an object" or item.endswith("is missing") or "must be" in item
    ]
    if not isinstance(raw, dict) or blocking:
        raise TelemetryError("; ".join(blocking or problems or ["unreadable event"]))
    known: set[str] = set(_ENVELOPE)
    extra: dict[str, Any] = {key: value for key, value in raw.items() if key not in known}
    payload: object = raw.get("payload")
    return Event(
        schema_version=_whole(raw.get("schema_version")) or 0,
        event_id=str(raw["event_id"]),
        event_type=str(raw["event_type"]),
        timestamp=str(raw["timestamp"]),
        source=str(raw["source"]),
        project_id=str(raw["project_id"]),
        occurrence_key=str(raw["occurrence_key"]),
        payload=dict(payload) if isinstance(payload, dict) else {},
        job_id=_text(raw.get("job_id")),
        run_id=_text(raw.get("run_id")),
        step_execution_id=_text(raw.get("step_execution_id")),
        agent_execution_id=_text(raw.get("agent_execution_id")),
        step=_text(raw.get("step")),
        attempt=_whole(raw.get("attempt")),
        round=_whole(raw.get("round")),
        factory_version=_text(raw.get("factory_version")),
        config_hash=_text(raw.get("config_hash")),
        extra=extra,
    )
