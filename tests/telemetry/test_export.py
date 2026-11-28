"""Export collapses a repeated occurrence. Prune keeps an active job and drops an old finished one."""

import json
from pathlib import Path

from modfactory.telemetry.export import Reader
from modfactory.telemetry.settings import TelemetrySettings


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _event(event_id: str, occurrence: str, stamp: str, job_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "event_type": "step.completed",
        "timestamp": stamp,
        "source": "core",
        "project_id": "p",
        "job_id": job_id,
        "run_id": None,
        "step_execution_id": None,
        "agent_execution_id": None,
        "step": "build",
        "attempt": 1,
        "round": 1,
        "factory_version": "0.2.1",
        "config_hash": "sha256:abc",
        "occurrence_key": occurrence,
        "payload": {"outcome": "READY"},
    }


def test_export_dedupes_and_prune_skips_active_jobs(tmp_path: Path) -> None:
    factory: Path = tmp_path / ".factory"
    finished: Path = factory / "jobs" / "001-done" / "telemetry" / "events.jsonl"
    active: Path = factory / "jobs" / "002-open" / "telemetry" / "events.jsonl"
    old: dict[str, object] = _event("same", "step.completed|001|0", "2000-01-01T00:00:00Z", "001-done")
    _write(finished, [old, dict(old)])
    (factory / "jobs" / "001-done" / "job.json").write_text(json.dumps({"id": "001-done", "step": "done"}) + "\n")
    kept: dict[str, object] = _event("open", "step.completed|002|0", "2000-01-01T00:00:00Z", "002-open")
    _write(active, [kept])
    (factory / "jobs" / "002-open" / "job.json").write_text(json.dumps({"id": "002-open", "step": "intake"}) + "\n")
    reader: Reader = Reader(factory, TelemetrySettings(retention_days=30))
    exported: list[str] = reader.jsonl(event_type="step.completed").splitlines()
    assert len(exported) == 2
    csv_text: str = reader.csv_text(event_type="step.completed")
    assert csv_text.splitlines()[0].startswith("schema_version,event_id,event_type")
    assert "outcome" in csv_text.splitlines()[0]
    notes: list[str] = reader.prune()
    assert any("002-open" in note for note in notes)
    assert finished.read_text() == ""
    assert json.loads(active.read_text())["event_id"] == "open"
