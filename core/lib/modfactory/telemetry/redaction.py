"""Allowlist first, then secret and path redaction. v1 never stores prompts or transcripts."""

import re
from typing import Any

from .schema import PAYLOAD_FIELDS
from .settings import TelemetrySettings

_FORBIDDEN: frozenset[str] = frozenset({
    "prompt", "prompts", "transcript", "transcripts", "tool_arguments", "tool_argument",
    "tool_output", "tool_outputs", "feedback", "feedback_text", "secret", "secrets",
    "token", "password", "authorization", "api_key", "body", "content", "text",
    "command_output", "env", "environment", "reason",
})
_AGENT: frozenset[str] = frozenset({"name", "agent_execution_id"})
_PRIVATE_KEY: re.Pattern[str] = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_SECRET: re.Pattern[str] = re.compile(
    r"(?i)(bearer\s+\S+|authorization:\s*\S+|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})"
)
_ABS_PATH: re.Pattern[str] = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


def looks_secret(value: str) -> bool:
    """True when a string itself is a credential or a private key."""
    return bool(_PRIVATE_KEY.search(value) or _SECRET.search(value))


def is_absolute_path(value: str) -> bool:
    """True for a filesystem path that would identify this machine."""
    return bool(_ABS_PATH.match(value))


def scrub_text(value: str) -> str:
    """Replace credential-shaped substrings. Absolute paths are dropped by the caller."""
    cleaned: str = _PRIVATE_KEY.sub("[redacted]", value)
    return _SECRET.sub("[redacted]", cleaned)


class Redactor:
    """Keeps only the payload fields declared for an event type."""

    def __init__(self, settings: TelemetrySettings) -> None:
        self._settings: TelemetrySettings = settings

    def payload(self, event_type: str, raw: dict[str, Any]) -> dict[str, Any]:
        allowed: frozenset[str] = PAYLOAD_FIELDS.get(event_type, frozenset())
        kept: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in allowed or key in _FORBIDDEN:
                continue
            cleaned: Any = self._value(key, value)
            if cleaned is not _drop:
                kept[key] = cleaned
        return kept

    def _value(self, key: str, value: Any) -> Any:
        if key == "agents" and isinstance(value, list):
            return [self._agent(item) for item in value if isinstance(item, dict)]
        if key == "plan_points" and isinstance(value, dict):
            return self._numbers(value)
        if key in {"models", "unpriced_models", "skipped", "adapters"} and isinstance(value, list):
            return [item for item in (self._scalar(piece) for piece in value) if isinstance(item, str)]
        if key == "roles" and isinstance(value, dict):
            return {
                name: digest for name, digest in (
                    (str(name), self._scalar(digest)) for name, digest in value.items()
                ) if isinstance(digest, str)
            }
        return self._scalar(value)

    def _agent(self, item: dict[str, Any]) -> dict[str, Any]:
        kept: dict[str, Any] = {}
        for key in _AGENT:
            cleaned: Any = self._scalar(item.get(key))
            if isinstance(cleaned, str):
                kept[key] = cleaned
        return kept

    def _numbers(self, value: dict[str, Any]) -> dict[str, float]:
        kept: dict[str, float] = {}
        for name, item in value.items():
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                continue
            kept[str(name)] = float(item)
        return kept

    def _scalar(self, value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value
        if not isinstance(value, str):
            return _drop
        if is_absolute_path(value):
            return None
        if self._settings.privacy["redact_secrets"] and looks_secret(value):
            return "[redacted]"
        if self._settings.privacy["redact_secrets"]:
            return scrub_text(value)
        return value


class _Drop:
    """Sentinel for a value that is not a scalar telemetry field."""


_drop: _Drop = _Drop()
