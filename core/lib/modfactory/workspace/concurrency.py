"""Workspace-wide cap on `new` and `resume` rows."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TypeAlias

from ..util import CORE, Refused

QueueRow: TypeAlias = tuple[str, str, str, str, str, str]


def default_concurrency() -> int | None:
    """Workspace-wide cap shipped in this factory's defaults.toml. None when that copy has no [workspace] table."""
    path: Path = CORE / "defaults.toml"
    parsed: object = tomllib.loads(path.read_text())
    if not isinstance(parsed, dict):
        return None
    table: object = parsed.get("workspace")
    if not isinstance(table, dict) or "concurrency" not in table:
        return None
    return parse_concurrency(table["concurrency"], f"{path}: [workspace].concurrency")


def parse_concurrency(value: object, where: str) -> int:
    """A workspace-wide cap: how many `new` and `resume` rows survive across every project."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Refused(f"{where} must be a whole number of at least 1 (got {value})")
    return value


def cap_rows(rows: list[QueueRow], limit: int) -> list[QueueRow]:
    """Keep the first `limit` actionable rows. Later `new` and `resume` rows become `hold`."""
    kept: int = 0
    capped: list[QueueRow] = []
    for project, action, issue, job, step, note in rows:
        if action in ("new", "resume"):
            if kept < limit:
                kept += 1
            else:
                action = "hold"
                note = f"workspace concurrency {limit} reached"
        capped.append((project, action, issue, job, step, note))
    return capped
