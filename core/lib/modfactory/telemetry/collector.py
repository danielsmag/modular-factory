"""Build a record and hand it to a sink. Storage failures stay inside telemetry."""

from typing import Any

from ..util import now, version
from .errors import TelemetryError
from .redaction import Redactor
from .schema import SCHEMA_VERSION, Event, stable_id
from .settings import TelemetrySettings
from .sink import EventSink, LocalJsonlSink


class Collector:
    """The only API the rest of the factory needs: ``emit``."""

    def __init__(self, sink: EventSink | None, settings: TelemetrySettings) -> None:
        self._sink: EventSink | None = sink
        self._settings: TelemetrySettings = settings
        self._redactor: Redactor = Redactor(settings)
        self._version: str = str(version())

    @property
    def settings(self) -> TelemetrySettings:
        return self._settings

    def emit(
        self,
        event_type: str,
        *,
        occurrence_key: str,
        payload: dict[str, Any],
        source: str = "core",
        job_id: str | None = None,
        run_id: str | None = None,
        step_execution_id: str | None = None,
        agent_execution_id: str | None = None,
        step: str | None = None,
        attempt: int | None = None,
        round: int | None = None,
        config_hash: str | None = None,
        project_id: str | None = None,
    ) -> None:
        """Record one event. Does nothing when this type is switched off."""
        if self._sink is None or not self._settings.allows(event_type):
            return
        try:
            event: Event = self._event(
                event_type,
                occurrence_key=occurrence_key,
                payload=payload,
                source=source,
                job_id=job_id,
                run_id=run_id,
                step_execution_id=step_execution_id,
                agent_execution_id=agent_execution_id,
                step=step,
                attempt=attempt,
                round=round,
                config_hash=config_hash,
                project_id=project_id,
            )
            self._sink.publish(event)
        except (TelemetryError, OSError, ValueError, TypeError) as err:
            self.note(err)

    def note(self, err: BaseException) -> None:
        """Remember a collection failure. Re-raise only when fail-open is off."""
        try:
            if isinstance(self._sink, LocalJsonlSink):
                self._sink.note_error(str(err), producer="telemetry")
        except (TelemetryError, OSError):
            pass
        if not self._settings.fail_open:
            raise TelemetryError(str(err)) from err

    def _event(self, event_type: str, **fields: Any) -> Event:
        payload: dict[str, Any] = self._redactor.payload(
            event_type, self._settings.project_payload(event_type, dict(fields.pop("payload"))),
        )
        project: str | None = fields.pop("project_id")
        if not project:
            if not isinstance(self._sink, LocalJsonlSink):
                raise TelemetryError("telemetry has no project id")
            project = self._sink.store.project_id()
        occurrence: str = str(fields.pop("occurrence_key"))
        return Event(
            schema_version=SCHEMA_VERSION,
            event_id=stable_id(occurrence),
            event_type=event_type,
            timestamp=now(),
            source=str(fields.pop("source")),
            project_id=project,
            occurrence_key=occurrence,
            payload=payload,
            job_id=fields["job_id"],
            run_id=fields["run_id"],
            step_execution_id=fields["step_execution_id"],
            agent_execution_id=fields["agent_execution_id"],
            step=fields["step"],
            attempt=fields["attempt"],
            round=fields["round"],
            factory_version=self._version,
            config_hash=fields["config_hash"],
        )
