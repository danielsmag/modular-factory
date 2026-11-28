"""When a planner scan is due."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..util import parse_time

INTERVAL_MINUTES: dict[str, int] = {"high": 5, "medium": 30, "low": 240}


def is_due(frequency: str, findings: dict[str, Any], snapshot_hash: str, at: datetime) -> bool:
    """Whether to scan at `at` (UTC). Nothing is due when the tickets are the ones the last scan saw."""
    if frequency == "manual":
        return False
    scanned_at: object = findings.get("scanned_at")
    if not isinstance(scanned_at, str) or not scanned_at:
        return True
    if findings.get("snapshot_hash") == snapshot_hash:
        return False
    try:
        since: datetime = parse_time(scanned_at)
    except ValueError:
        return True
    minutes: float = (at - since).total_seconds() / 60
    return minutes >= INTERVAL_MINUTES.get(frequency, INTERVAL_MINUTES["medium"])
