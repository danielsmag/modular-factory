"""Append-only JSONL, manifests, and the stable project id. Writes are locked."""

import fcntl
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..util import now, write_atomic
from .errors import TelemetryError
from .schema import SCHEMA_VERSION

_HEALTH_LIMIT: int = 40


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive lock for one telemetry file. The lock file sits beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: Any = open(path, "a")  # noqa: SIM115
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def repair_tail(path: Path) -> int:
    """Drop an incomplete trailing record. Complete lines, valid or not, stay for validation."""
    if not path.is_file():
        return 0
    try:
        raw: bytes = path.read_bytes()
    except OSError as err:
        raise TelemetryError(f"could not read {path.name}") from err
    if not raw or raw.endswith(b"\n"):
        return 0
    head: bytes
    tail: bytes
    head, _sep, tail = raw.rpartition(b"\n")
    try:
        decoded: object = json.loads(tail.decode("utf-8", errors="replace"))
    except ValueError:
        decoded = None
    if isinstance(decoded, dict):
        write_atomic(path, raw.decode("utf-8", errors="replace") + "\n")
        return 0
    kept: bytes = head + b"\n" if head else b""
    write_atomic(path, kept.decode("utf-8", errors="replace"))
    return len(raw) - len(kept)


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Decoded objects, and a problem string per malformed line."""
    if not path.is_file():
        return [], []
    try:
        text: str = path.read_text(errors="replace")
    except OSError as err:
        raise TelemetryError(f"could not read {path.name}") from err
    events: list[dict[str, Any]] = []
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            decoded: object = json.loads(line)
        except ValueError:
            problems.append(f"{path.name}:{number}: malformed record")
            continue
        if not isinstance(decoded, dict):
            problems.append(f"{path.name}:{number}: event is not an object")
            continue
        events.append(decoded)
    return events, problems


class TelemetryStore:
    """Files under ``.factory/telemetry`` and ``.factory/jobs/<id>/telemetry``."""

    def __init__(self, factory_dir: Path) -> None:
        self.factory_dir: Path = factory_dir
        self.root: Path = factory_dir / "telemetry"
        self._project_id: str | None = None

    def project_id(self) -> str:
        """The stable id for this checkout. Created once. Not the repository name."""
        if self._project_id:
            return self._project_id
        path: Path = self.root / "project.json"
        with locked(self.root / ".lock"):
            existing: dict[str, Any] | None = _load_json(path) if path.is_file() else None
            found: object = existing.get("project_id") if existing else None
            if isinstance(found, str) and found.strip():
                self._project_id = found
                return found
            if path.is_file():
                raise TelemetryError("project.json is unreadable")
            created: str = str(uuid.uuid4())
            write_atomic(path, json.dumps({
                "schema_version": SCHEMA_VERSION,
                "project_id": created,
                "created_at": now(),
            }, indent=2) + "\n")
            self._project_id = created
            return created

    def read_project_id(self) -> str | None:
        """The id on disk, or None when telemetry has never been written."""
        loaded: dict[str, Any] | None = _load_json(self.root / "project.json")
        found: object = loaded.get("project_id") if loaded else None
        if isinstance(found, str) and found.strip():
            return found
        return None

    def event_path(self, job_id: str | None) -> Path:
        if job_id:
            return self.factory_dir / "jobs" / job_id / "telemetry" / "events.jsonl"
        return self.root / "events.jsonl"

    def manifest_path(self, job_id: str | None) -> Path:
        if job_id:
            return self.factory_dir / "jobs" / job_id / "telemetry" / "manifest.json"
        return self.root / "manifest.json"

    def snapshot_path(self, digest: str) -> Path:
        safe: str = digest.removeprefix("sha256:")
        return self.root / "snapshots" / f"config-{safe}.json"

    def append(self, job_id: str | None, record: dict[str, Any]) -> bool:
        """Append one record unless its occurrence was already stored. True when a line was written."""
        path: Path = self.event_path(job_id)
        with locked(path.with_suffix(".jsonl.lock")):
            removed: int = repair_tail(path)
            if removed:
                self.note_repair(path.name, removed)
            manifest: dict[str, Any] = self._manifest(job_id, path)
            key: object = record.get("occurrence_key")
            seen: list[str] = _strings(manifest.get("occurrences"))
            if isinstance(key, str) and key in seen:
                return False
            line: str = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            if isinstance(key, str):
                seen.append(key)
                manifest["occurrences"] = seen
                self._write_manifest(job_id, manifest)
            return True

    def manifest(self, job_id: str | None) -> dict[str, Any]:
        path: Path = self.event_path(job_id)
        with locked(path.with_suffix(".jsonl.lock")):
            return self._manifest(job_id, path)

    def update_manifest(self, job_id: str | None, change: dict[str, Any]) -> None:
        path: Path = self.event_path(job_id)
        with locked(path.with_suffix(".jsonl.lock")):
            manifest: dict[str, Any] = self._manifest(job_id, path)
            manifest.update(change)
            self._write_manifest(job_id, manifest)

    def note_error(self, message: str, *, producer: str = "telemetry") -> None:
        self._note("errors", {"at": now(), "producer": producer, "message": _safe(message)})

    def note_repair(self, name: str, dropped_bytes: int) -> None:
        self._note("repairs", {"at": now(), "file": name, "dropped_bytes": dropped_bytes})

    def health(self) -> dict[str, Any]:
        loaded: dict[str, Any] | None = _load_json(self.root / "health.json")
        if loaded is None:
            return {"schema_version": SCHEMA_VERSION, "errors": [], "repairs": []}
        return loaded

    def job_event_files(self) -> list[Path]:
        jobs: Path = self.factory_dir / "jobs"
        if not jobs.is_dir():
            return []
        found: list[Path] = []
        for child in sorted(jobs.iterdir()):
            path: Path = child / "telemetry" / "events.jsonl"
            if path.is_file():
                found.append(path)
        return found

    def _manifest(self, job_id: str | None, events_path: Path) -> dict[str, Any]:
        path: Path = self.manifest_path(job_id)
        loaded: dict[str, Any] | None = _load_json(path) if path.is_file() else None
        manifest: dict[str, Any] = loaded or {
            "schema_version": SCHEMA_VERSION,
            "occurrences": [],
            "transitions": [],
            "runs": [],
            "usage": {},
            "config_hash": None,
        }
        if not _strings(manifest.get("occurrences")) and events_path.is_file() and events_path.stat().st_size:
            rows, _problems = read_jsonl(events_path)
            manifest["occurrences"] = [
                row["occurrence_key"] for row in rows if isinstance(row.get("occurrence_key"), str)
            ]
        return manifest

    def _write_manifest(self, job_id: str | None, manifest: dict[str, Any]) -> None:
        path: Path = self.manifest_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    def _note(self, key: str, entry: dict[str, Any]) -> None:
        path: Path = self.root / "health.json"
        try:
            with locked(self.root / ".lock"):
                loaded: dict[str, Any] | None = _load_json(path)
                body: dict[str, Any] = loaded or {
                    "schema_version": SCHEMA_VERSION, "errors": [], "repairs": [],
                }
                rows: list[Any] = list(body.get(key) or [])
                rows.append(entry)
                body[key] = rows[-_HEALTH_LIMIT:]
                path.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(path, json.dumps(body, indent=2) + "\n")
        except (OSError, TelemetryError):
            return


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _safe(message: str) -> str:
    text: str = message.replace("\n", " ")
    if len(text) > 300:
        text = text[:300]
    return text
