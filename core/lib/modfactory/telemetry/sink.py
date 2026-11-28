"""Where events go. v1 ships the local JSONL sink. A future Hub sink publishes the same records."""

from typing import Any, Protocol

from .schema import Event
from .storage import TelemetryStore


class EventSink(Protocol):
    """One destination for telemetry records."""

    def publish(self, event: Event) -> None:
        """Store one event. Raising does not roll back job state."""


class NullSink:
    """Drops every event. Used when collection is off."""

    def publish(self, event: Event) -> None:
        return


class LocalJsonlSink:
    """Append-only JSONL under ``.factory``. Job events stay with the job."""

    def __init__(self, factory_dir: Any) -> None:
        self.store: TelemetryStore = factory_dir if isinstance(factory_dir, TelemetryStore) else TelemetryStore(
            factory_dir)

    def publish(self, event: Event) -> None:
        self.store.append(event.job_id, event.to_dict())

    def note_error(self, message: str, *, producer: str = "telemetry") -> None:
        self.store.note_error(message, producer=producer)
