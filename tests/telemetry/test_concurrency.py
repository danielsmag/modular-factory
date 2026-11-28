"""Two processes can append to one JSONL file without tearing a line."""

from multiprocessing import get_context
from pathlib import Path

from modfactory.telemetry.collector import Collector
from modfactory.telemetry.settings import TelemetrySettings
from modfactory.telemetry.sink import LocalJsonlSink
from modfactory.telemetry.storage import read_jsonl


def _append(factory: str, job_id: str, start: int) -> None:
    collector: Collector = Collector(LocalJsonlSink(Path(factory)), TelemetrySettings())
    index: int
    for index in range(start, start + 20):
        collector.emit(
            "job.created",
            occurrence_key=f"job.created|{job_id}|{index}",
            payload={"pipeline": "default"},
            job_id=job_id,
        )


def test_two_processes_append_whole_records(tmp_path: Path) -> None:
    factory: Path = tmp_path / ".factory"
    context = get_context("spawn")
    first = context.Process(target=_append, args=(str(factory), "001-add", 0))
    second = context.Process(target=_append, args=(str(factory), "001-add", 20))
    first.start()
    second.start()
    first.join()
    second.join()
    assert first.exitcode == 0 and second.exitcode == 0
    rows, problems = read_jsonl(factory / "jobs" / "001-add" / "telemetry" / "events.jsonl")
    assert problems == []
    assert len(rows) == 40
    assert len({row["event_id"] for row in rows}) == 40
