"""``[telemetry]`` as the collector reads it. Validation lives in ``config.py``."""

from typing import Any

COLLECT_NAMES: tuple[str, ...] = (
    "lifecycle",
    "performance",
    "usage",
    "cost",
    "checks",
    "reviews",
    "human_feedback",
    "git_metrics",
    "config_fingerprints",
    "artifact_metadata",
)
PRIVACY_NAMES: tuple[str, ...] = (
    "store_prompts",
    "store_transcripts",
    "store_tool_arguments",
    "store_tool_outputs",
    "store_config_content",
    "store_feedback_text",
    "redact_secrets",
)
# v1 has no producer for these. Turning a flag on does not start capture.
CAPTURE_FLAGS: frozenset[str] = frozenset({
    "store_prompts",
    "store_transcripts",
    "store_tool_arguments",
    "store_tool_outputs",
    "store_feedback_text",
})
_EVENT_COLLECT: dict[str, str] = {
    "job.created": "lifecycle",
    "job.completed": "lifecycle",
    "run.started": "lifecycle",
    "run.ended": "lifecycle",
    "step.entered": "lifecycle",
    "step.dispatched": "lifecycle",
    "step.completed": "lifecycle",
    "step.transitioned": "lifecycle",
    "step.skipped": "lifecycle",
    "step.interrupted": "lifecycle",
    "agent.started": "lifecycle",
    "agent.completed": "lifecycle",
    "error.occurred": "lifecycle",
    "human.requested": "human_feedback",
    "human.responded": "human_feedback",
    "check.completed": "checks",
    "review.completed": "reviews",
    "usage.recorded": "usage",
    "config.snapshot": "config_fingerprints",
    "artifact.recorded": "artifact_metadata",
}
_DURATION: frozenset[str] = frozenset({"duration_ms", "duration_kind", "duration_interval"})
_GIT: frozenset[str] = frozenset({
    "base_commit", "head_commit", "files_changed", "insertions", "deletions",
})
_COST: frozenset[str] = frozenset({
    "cost_usd", "delta_cost_usd", "currency", "price_table_version", "unpriced_models",
})
_TOKENS: frozenset[str] = frozenset({
    "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
    "delta_input_tokens", "delta_output_tokens", "models",
})


class TelemetrySettings:
    """Effective telemetry settings. Missing keys use the v1 defaults."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        storage: str = "jsonl",
        retention_days: int = 90,
        fail_open: bool = True,
        collect: dict[str, bool] | None = None,
        privacy: dict[str, bool] | None = None,
    ) -> None:
        self.enabled: bool = enabled
        self.storage: str = storage
        self.retention_days: int = retention_days
        self.fail_open: bool = fail_open
        self.collect: dict[str, bool] = {name: True for name in COLLECT_NAMES}
        self.privacy: dict[str, bool] = {name: False for name in PRIVACY_NAMES}
        self.privacy["redact_secrets"] = True
        if collect:
            self.collect.update({key: value for key, value in collect.items() if key in self.collect})
        if privacy:
            self.privacy.update({key: value for key, value in privacy.items() if key in self.privacy})

    def allows(self, event_type: str) -> bool:
        """True when this event type's collection flag is on."""
        if not self.enabled or self.storage != "jsonl":
            return False
        flag: str | None = _EVENT_COLLECT.get(event_type)
        if flag is None:
            return False
        if event_type == "usage.recorded":
            return self.collect["usage"] or self.collect["cost"]
        return self.collect.get(flag, False)

    def project_payload(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Drop fields whose collection flag is off. Allowlisting happens later."""
        kept: dict[str, Any] = dict(payload)
        if not self.collect["performance"]:
            for key in _DURATION:
                kept.pop(key, None)
        if not self.collect["git_metrics"]:
            for key in _GIT:
                kept.pop(key, None)
        if event_type == "usage.recorded":
            if not self.collect["cost"]:
                for key in _COST:
                    kept.pop(key, None)
            if not self.collect["usage"]:
                for key in _TOKENS:
                    kept.pop(key, None)
                kept.pop("plan_points", None)
                kept.pop("usage_share_percent", None)
        return kept


def _bool_map(raw: object, names: tuple[str, ...], fallback: dict[str, bool]) -> dict[str, bool]:
    found: dict[str, bool] = dict(fallback)
    if not isinstance(raw, dict):
        return found
    for name in names:
        value: object = raw.get(name, found[name])
        if isinstance(value, bool):
            found[name] = value
    return found


def settings_from(cfg: Any) -> TelemetrySettings:
    """Settings from a loaded config. A missing ``[telemetry]`` table uses defaults."""
    data: object = getattr(cfg, "data", {})
    raw: object = data.get("telemetry", {}) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    enabled: object = raw.get("enabled", True)
    storage: object = raw.get("storage", "jsonl")
    retention: object = raw.get("retention_days", 90)
    fail_open: object = raw.get("fail_open", True)
    collect_fallback: dict[str, bool] = {name: True for name in COLLECT_NAMES}
    privacy_fallback: dict[str, bool] = {name: False for name in PRIVACY_NAMES}
    privacy_fallback["redact_secrets"] = True
    return TelemetrySettings(
        enabled=enabled if isinstance(enabled, bool) else True,
        storage=storage if isinstance(storage, str) else "jsonl",
        retention_days=retention if isinstance(retention, int) and not isinstance(retention, bool) else 90,
        fail_open=fail_open if isinstance(fail_open, bool) else True,
        collect=_bool_map(raw.get("collect"), COLLECT_NAMES, collect_fallback),
        privacy=_bool_map(raw.get("privacy"), PRIVACY_NAMES, privacy_fallback),
    )
