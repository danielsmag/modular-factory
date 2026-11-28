"""Envelope rules: stable ids, null stays null, unknown fields and versions are reported."""

from modfactory.telemetry.schema import SCHEMA_VERSION, Event, event_from_dict, schema_problems, stable_id


def test_envelope_round_trip_keeps_nulls_and_unknown_fields() -> None:
    occurrence: str = "step.completed|001|0|build|READY|2026-01-01T00:00:00Z|gate"
    event: Event = Event(
        schema_version=SCHEMA_VERSION,
        event_id=stable_id(occurrence),
        event_type="step.completed",
        timestamp="2026-01-01T00:00:00Z",
        source="core",
        project_id="project-1",
        occurrence_key=occurrence,
        payload={"outcome": "READY", "duration_ms": None},
        job_id="001-add",
        step="build",
        attempt=2,
        round=1,
        config_hash="sha256:abc",
    )
    repeat: Event = Event(
        schema_version=SCHEMA_VERSION,
        event_id=stable_id(occurrence),
        event_type="step.completed",
        timestamp="2026-01-01T00:00:01Z",
        source="core",
        project_id="project-1",
        occurrence_key=occurrence,
        payload={"outcome": "READY"},
        job_id="001-add",
        step="build",
        config_hash="sha256:abc",
    )
    assert event.event_id == repeat.event_id
    raw: dict[str, object] = event.to_dict()
    raw["future_field"] = {"kept": True}
    loaded: Event = event_from_dict(raw)
    assert loaded.payload["duration_ms"] is None
    assert loaded.payload["duration_ms"] != 0
    assert loaded.extra["future_field"] == {"kept": True}
    raw["schema_version"] = 99
    assert "unsupported schema_version 99" in schema_problems(raw)
    assert event_from_dict(raw).schema_version == 99
