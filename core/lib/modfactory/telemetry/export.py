"""Read local telemetry back: status, validation, export, and explicit prune."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..graph import END_STATES
from ..util import parse_time, write_atomic
from .schema import CSV_ENVELOPE, PAYLOAD_FIELDS, schema_problems
from .settings import TelemetrySettings
from .storage import TelemetryStore, locked, read_jsonl, repair_tail
from .usage import PROVIDER_NOTES


class Reader:
    """Read-only view of telemetry already on disk. Does not create files."""

    def __init__(self, factory_dir: Path, settings: TelemetrySettings) -> None:
        self.factory_dir: Path = factory_dir
        self.settings: TelemetrySettings = settings
        self.store: TelemetryStore = TelemetryStore(factory_dir)

    def records(
        self, job_id: str | None = None, event_type: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Events in write order, plus malformed-line notes. Duplicates are kept here."""
        paths: list[Path] = [self.store.event_path(job_id)] if job_id else self._all_paths()
        events: list[dict[str, Any]] = []
        problems: list[str] = []
        for path in paths:
            if not path.is_file():
                continue
            rows, bad = read_jsonl(path)
            problems.extend(f"{path.parent.parent.name}: {item}" if job_id is None else item for item in bad)
            for row in rows:
                if event_type is None or row.get("event_type") == event_type:
                    events.append(row)
        return events, problems

    def deduped(
        self, job_id: str | None = None, event_type: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Drop repeated event ids and occurrence keys. The first copy wins."""
        rows, problems = self.records(job_id, event_type)
        kept: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        for row in rows:
            event_id: object = row.get("event_id")
            occurrence: object = row.get("occurrence_key")
            if isinstance(event_id, str) and event_id in seen_ids:
                problems.append(f"duplicate event_id {event_id}")
                continue
            if isinstance(occurrence, str) and occurrence in seen_keys:
                problems.append(f"duplicate occurrence {occurrence}")
                continue
            if isinstance(event_id, str):
                seen_ids.add(event_id)
            if isinstance(occurrence, str):
                seen_keys.add(occurrence)
            kept.append(row)
        return kept, problems

    def validate(self) -> list[str]:
        """Malformed records, duplicates, schema problems, and missing job folders."""
        rows, problems = self.records()
        seen: set[str] = set()
        for row in rows:
            event_id: object = row.get("event_id")
            if isinstance(event_id, str):
                if event_id in seen:
                    problems.append(f"duplicate event_id {event_id}")
                seen.add(event_id)
            for problem in schema_problems(row):
                label: object = row.get("event_type") or event_id or "event"
                problems.append(f"{label}: {problem}")
            job_id: object = row.get("job_id")
            if isinstance(job_id, str) and not (self.factory_dir / "jobs" / job_id / "job.json").is_file():
                problems.append(f"{job_id}: job.json is missing")
        return problems

    def status_text(self) -> str:
        project: str | None = self.store.read_project_id()
        rows, problems = self.records()
        health: dict[str, Any] = self.store.health() if (self.store.root / "health.json").is_file() else {}
        errors: list[Any] = list(health.get("errors") or [])
        stamps: list[str] = [row["timestamp"] for row in rows if isinstance(row.get("timestamp"), str)]
        lines: list[str] = [
            f"telemetry: {'enabled' if self.settings.enabled else 'disabled'}",
            f"storage: {self.settings.storage}",
            f"fail_open: {str(self.settings.fail_open).lower()}",
            f"project_id: {project or 'none'}",
            f"retention_days: {self.settings.retention_days}",
            f"events: {len(rows)}",
            f"jobs: {len(self.store.job_event_files())}",
            f"malformed: {len(problems)}",
            f"oldest: {min(stamps) if stamps else 'none'}",
            f"newest: {max(stamps) if stamps else 'none'}",
            "health: ok" if not errors else f"health: {len(errors)} collection error(s)",
        ]
        for entry in errors[-5:]:
            if isinstance(entry, dict) and isinstance(entry.get("message"), str):
                lines.append(f"  - {entry['message']}")
        lines.append(
            "usage: " + "; ".join(f"{name}: {note}" for name, note in PROVIDER_NOTES.items())
        )
        lines.append("cost_kind estimated is a price-table estimate; unavailable is null, not zero")
        lines.append("cumulative_snapshot usage must not be summed; use delta_* or the latest snapshot")
        return "\n".join(lines) + "\n"

    def jsonl(self, job_id: str | None = None, event_type: str | None = None) -> str:
        rows, _problems = self.deduped(job_id, event_type)
        return "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)

    def csv_text(self, job_id: str | None = None, event_type: str | None = None) -> str:
        rows, _problems = self.deduped(job_id, event_type)
        extra: tuple[str, ...] = tuple(sorted(PAYLOAD_FIELDS.get(event_type, ()))) if event_type else ()
        header: list[str] = list(CSV_ENVELOPE) + (list(extra) if extra else ["payload"])
        buffer: io.StringIO = io.StringIO()
        writer: csv.DictWriter[str] = csv.DictWriter(
            buffer, fieldnames=header, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            flat: dict[str, Any] = {key: row.get(key) for key in CSV_ENVELOPE}
            payload: object = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if extra and isinstance(payload, dict):
                for key in extra:
                    flat[key] = payload.get(key)
            else:
                flat["payload"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            writer.writerow({key: "" if flat.get(key) is None else flat.get(key) for key in header})
        return buffer.getvalue()

    def prune(self, *, dry_run: bool = False) -> list[str]:
        """Drop events older than retention. Active jobs and unreadable job files are left alone."""
        cutoff: datetime = datetime.now(UTC) - timedelta(days=self.settings.retention_days)
        notes: list[str] = []
        for path in self._all_paths():
            if not path.is_file():
                continue
            job_id: str | None = _job_id(path)
            if job_id is not None and _active(self.factory_dir, job_id):
                notes.append(f"kept active job {job_id}")
                continue
            removed: int = self._prune_file(path, job_id, cutoff, dry_run)
            notes.append(f"{path.name}: removed {removed}" + (" (dry run)" if dry_run else ""))
        return notes

    def _prune_file(self, path: Path, job_id: str | None, cutoff: datetime, dry_run: bool) -> int:
        with locked(path.with_suffix(".jsonl.lock")):
            repair_tail(path)
            rows, _problems = read_jsonl(path)
            kept: list[dict[str, Any]] = []
            removed: int = 0
            for row in rows:
                stamp: object = row.get("timestamp")
                if isinstance(stamp, str) and _older(stamp, cutoff):
                    removed += 1
                    continue
                kept.append(row)
            if dry_run or removed == 0:
                return removed
            body: str = "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in kept)
            write_atomic(path, body)
            manifest: dict[str, Any] = self.store._manifest(job_id, path)  # already holding this file's lock
            manifest["occurrences"] = [
                row["occurrence_key"] for row in kept if isinstance(row.get("occurrence_key"), str)
            ]
            self.store._write_manifest(job_id, manifest)
            return removed

    def _all_paths(self) -> list[Path]:
        paths: list[Path] = []
        project: Path = self.store.event_path(None)
        if project.is_file():
            paths.append(project)
        paths.extend(self.store.job_event_files())
        return paths


def _job_id(path: Path) -> str | None:
    """``.../jobs/<id>/telemetry/events.jsonl`` belongs to that job. The project log does not."""
    if path.parent.name != "telemetry" or path.parent.parent.name == "telemetry":
        return None
    if path.parent.parent.parent.name != "jobs":
        return None
    return path.parent.parent.name


def _active(factory_dir: Path, job_id: str) -> bool:
    path: Path = factory_dir / "jobs" / job_id / "job.json"
    if not path.is_file():
        return True
    try:
        loaded: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return True
    if not isinstance(loaded, dict):
        return True
    return loaded.get("step") not in END_STATES


def _older(stamp: str, cutoff: datetime) -> bool:
    try:
        return parse_time(stamp) < cutoff
    except (TypeError, ValueError):
        return False
