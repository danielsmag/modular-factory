"""A real job records lifecycle events. Collection failure and disabled mode leave the job alone."""

import json
import shutil
from pathlib import Path

from conftest import World
from modfactory.telemetry.storage import read_jsonl


def test_create_records_lifecycle_without_private_text(world: World) -> None:
    number: int = world.issue(title="Add dark mode")
    job: str = world.create(number)
    path: Path = world.dir(job) / "telemetry" / "events.jsonl"
    rows, problems = read_jsonl(path)
    assert problems == []
    types: list[object] = [row["event_type"] for row in rows]
    assert "job.created" in types
    assert "run.started" in types
    assert "run.ended" in types
    assert "config.snapshot" in types
    assert "step.entered" in types
    entered: dict[str, object] = next(row for row in rows if row["event_type"] == "step.entered")
    assert entered["job_id"] == job
    assert entered["run_id"]
    assert entered["step_execution_id"]
    assert entered["config_hash"]
    payload: object = entered["payload"]
    assert isinstance(payload, dict)
    assert payload.get("resolved_model") is None
    dumped: str = json.dumps(rows)
    assert "Please add it." not in dumped
    assert "sk-" not in dumped
    for row in rows:
        if row["event_type"] != "usage.recorded":
            continue
        usage: object = row["payload"]
        assert isinstance(usage, dict)
        assert usage["cost_usd"] is None
        assert usage["input_tokens"] is None

    world.configure("[telemetry]\nenabled = false\n")
    other: str = world.create(world.issue(title="Add another"))
    assert not (world.dir(other) / "telemetry").exists()

    blocked: Path = world.repo / ".factory" / "telemetry"
    shutil.rmtree(blocked, ignore_errors=True)
    blocked.write_text("not a directory\n")
    world.configure("")
    third: str = world.create(world.issue(title="Add a third"))
    assert world.job(third)["step"] == "intake"
