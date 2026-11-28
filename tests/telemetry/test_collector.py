"""Disabled collection writes nothing. A sink error does not escape when fail-open is on."""

from pathlib import Path

from modfactory.telemetry.collector import Collector
from modfactory.telemetry.schema import Event
from modfactory.telemetry.settings import TelemetrySettings
from modfactory.telemetry.sink import LocalJsonlSink
from modfactory.telemetry.storage import TelemetryStore


class _Boom:
    def publish(self, event: Event) -> None:
        raise OSError("disk full")


def test_disabled_is_silent_and_sink_errors_stay_inside(tmp_path: Path) -> None:
    quiet: Collector = Collector(None, TelemetrySettings(enabled=False))
    quiet.emit("job.created", occurrence_key="job.created|1", payload={"pipeline": "default"}, job_id="001")
    assert not (tmp_path / ".factory").exists()

    store: TelemetryStore = TelemetryStore(tmp_path / ".factory")
    sink: LocalJsonlSink = LocalJsonlSink(store)
    live: Collector = Collector(sink, TelemetrySettings())
    live.emit("job.created", occurrence_key="job.created|001", payload={"pipeline": "default"}, job_id="001-add")
    sink.publish = _Boom().publish  # type: ignore[method-assign]
    live.emit("job.created", occurrence_key="job.created|002", payload={"pipeline": "default"}, job_id="001-add")
    errors: object = store.health().get("errors")
    assert isinstance(errors, list) and errors
    assert len(store.event_path("001-add").read_text().splitlines()) == 1
    assert "disk full" in str(errors[-1])
