"""Failures inside telemetry. Callers decide whether a job may continue."""


class TelemetryError(Exception):
    """A telemetry read or write failed. Job state is unchanged by this error."""

    def __init__(self, message: str, *, producer: str = "telemetry") -> None:
        self.producer: str = producer
        super().__init__(message)
