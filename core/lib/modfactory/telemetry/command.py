"""``mfactory telemetry``: status, events, validate, export, and prune."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..store import Store
from ..util import Refused
from .export import Reader
from .settings import settings_from


def run(cfg: Any, args: argparse.Namespace) -> int:
    """Run one telemetry subcommand. Returns the process status."""
    reader: Reader = Reader(Path(cfg.repo) / ".factory", settings_from(cfg))
    what: str | None = args.telemetry_what if isinstance(args.telemetry_what, str) else None
    if what == "status":
        sys.stdout.write(reader.status_text())
        return 0
    if what == "events":
        job_id: str = Store(cfg).find(args.job)
        rows, problems = reader.records(job_id, _optional(args, "event_type"))
        for problem in problems:
            print(f"note: {problem}", file=sys.stderr)
        sys.stdout.write("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
        return 0
    if what == "validate":
        found: list[str] = reader.validate()
        if not found:
            print("telemetry validate: ok")
            return 0
        print(f"telemetry validate: {len(found)} problem(s)")
        for problem in found:
            print(f"  - {problem}")
        return 1
    if what == "export":
        chosen: str | None = Store(cfg).find(args.job) if _optional(args, "job") else None
        text: str = (
            reader.csv_text(chosen, _optional(args, "event_type"))
            if args.format == "csv"
            else reader.jsonl(chosen, _optional(args, "event_type"))
        )
        if _optional(args, "output"):
            Path(str(args.output)).write_text(text)
        else:
            sys.stdout.write(text)
        return 0
    if what == "prune":
        for note in reader.prune(dry_run=bool(args.dry_run)):
            print(note)
        return 0
    raise Refused(f"unknown telemetry command {what!r}")


def _optional(args: argparse.Namespace, name: str) -> str | None:
    value: object = getattr(args, name, None)
    if isinstance(value, str) and value:
        return value
    return None
