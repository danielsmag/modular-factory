"""Append, and repair only an incomplete trailing line."""

import json
from pathlib import Path

from modfactory.telemetry.schema import Event
from modfactory.telemetry.sink import LocalJsonlSink
from modfactory.telemetry.storage import TelemetryStore, read_jsonl, repair_tail


def test_append_keeps_valid_lines_and_drops_a_partial_tail(tmp_path: Path) -> None:
    store: TelemetryStore = TelemetryStore(tmp_path / ".factory")
    sink: LocalJsonlSink = LocalJsonlSink(store)
    event: Event = Event(
        schema_version=1, event_id="1", event_type="job.created", timestamp="2026-01-01T00:00:00Z",
        source="core", project_id="p", occurrence_key="job.created|001", payload={"pipeline": "default"},
        job_id="001-add",
    )
    sink.publish(event)
    sink.publish(event)
    path: Path = store.event_path("001-add")
    assert len(path.read_text().splitlines()) == 1
    path.write_bytes(path.read_bytes() + b'{"event_id": "partial"')
    removed: int = repair_tail(path)
    assert removed > 0
    rows, problems = read_jsonl(path)
    assert problems == []
    assert json.loads(path.read_text())["occurrence_key"] == "job.created|001"
    assert rows[0]["event_id"] == "1"
