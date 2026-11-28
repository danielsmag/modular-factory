"""Normalize adapter usage. Cumulative snapshots must not be summed; deltas may."""

import json
import os
import re
from pathlib import Path
from typing import Any

from .. import cost
from .fingerprint import canonical, digest

PROVIDER_NOTES: dict[str, str] = {
    "claude": "tokens and estimated cost from Claude Code transcripts when the prompt names the job folder",
    "cursor": "tokens when a Cursor transcript message has a usage object; no price table, so cost is null",
    "codex": "no usage interface in this factory; token and cost fields stay null",
}
_TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
)


def price_table_version(prices: object) -> str | None:
    """Identity of the price table used for an estimate. Not a provider invoice."""
    if not isinstance(prices, dict) or not prices:
        return None
    return digest(prices)


def claude_snapshots(
    cfg: Any, job: dict[str, Any], job_dir: Path, previous: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Cumulative Claude usage per step, plus the attribution memory to store."""
    if "claude" not in cfg.tools:
        return [], previous
    rows: list[dict[str, Any]] | None = cost.claude_usage(cfg, job, job_dir)
    prices: object = cfg.adapters.get("claude", {}).get("prices", {}) if isinstance(cfg.adapters, dict) else {}
    version: str | None = price_table_version(prices)
    if not rows:
        payload: dict[str, Any] = _unavailable("claude", "claude_transcript")
        key: str = f"{job['id']}|{_step(job)}|claude_transcript|unavailable"
        return _once(previous, key, payload), previous
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        step: str = str(row.get("step") or "other")
        bucket: dict[str, Any] = grouped.setdefault(step, _blank())
        _add_row(bucket, row)
    events: list[tuple[str, dict[str, Any]]] = []
    memory: dict[str, Any] = dict(previous)
    for step, bucket in sorted(grouped.items()):
        attribution: str = f"{job['id']}|{step}|claude_transcript"
        payload = _cumulative(
            "claude", "claude_transcript", attribution, bucket, memory.get(attribution), version,
        )
        occurrence: str = f"usage.recorded|{attribution}|{canonical(_totals(bucket))}"
        if payload is None:
            continue
        events.append((occurrence, payload))
        memory[attribution] = _totals(bucket)
    return events, memory


def cursor_snapshots(
    cfg: Any, job: dict[str, Any], job_dir: Path, previous: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Cursor token totals when a transcript exposes them. Cost stays null."""
    if "cursor" not in cfg.tools:
        return [], previous
    rows: list[dict[str, Any]] | None = _cursor_rows(cfg.repo, job, job_dir)
    if not rows:
        payload: dict[str, Any] = _unavailable("cursor", "cursor_transcript")
        key: str = f"{job['id']}|{_step(job)}|cursor_transcript|unavailable"
        return _once(previous, key, payload), previous
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket: dict[str, Any] = grouped.setdefault(str(row.get("step") or "other"), _blank())
        _add_row(bucket, row)
    events: list[tuple[str, dict[str, Any]]] = []
    memory: dict[str, Any] = dict(previous)
    for step, bucket in sorted(grouped.items()):
        attribution: str = f"{job['id']}|{step}|cursor_transcript"
        payload = _cumulative("cursor", "cursor_transcript", attribution, bucket, memory.get(attribution), None)
        if payload is None:
            continue
        events.append((f"usage.recorded|{attribution}|{canonical(_totals(bucket))}", payload))
        memory[attribution] = _totals(bucket)
    return events, memory


def codex_unavailable(cfg: Any, job: dict[str, Any], previous: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Codex does not report tokens or cost to this factory."""
    if "codex" not in cfg.tools:
        return []
    key: str = f"{job['id']}|{_step(job)}|codex|unavailable"
    return _once(previous, key, _unavailable("codex", "unavailable"))


def plan_snapshots(
    job: dict[str, Any], previous: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Plan-limit points already accumulated on the job. They are not dollars."""
    raw: object = job.get("plan_by_step")
    if not isinstance(raw, dict):
        return [], previous
    events: list[tuple[str, dict[str, Any]]] = []
    memory: dict[str, Any] = dict(previous)
    for step, points in raw.items():
        if not isinstance(points, dict):
            continue
        attribution: str = f"{job['id']}|{step}|plan_statusline"
        copied: dict[str, Any] = {
            name: value for name, value in points.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        totals: dict[str, Any] = {"plan_points": copied}
        if memory.get(attribution) == totals:
            continue
        events.append((f"usage.recorded|{attribution}|{canonical(copied)}", {
            "provider": "claude",
            "source": "plan_statusline",
            "cost_kind": "observed",
            "cost_usd": None,
            "currency": None,
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "aggregation": "cumulative_snapshot",
            "attribution_key": attribution,
            "plan_points": copied,
            "delta_cost_usd": None,
            "price_table_version": None,
            "usage_share_percent": None,
            "models": [],
            "unpriced_models": [],
            "delta_input_tokens": None,
            "delta_output_tokens": None,
        }))
        memory[attribution] = totals
    return events, memory


def share_snapshots(
    job: dict[str, Any], previous: dict[str, Any],
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """Each step's share of this machine's recent Claude Code usage. Not a job cost."""
    raw: object = job.get("usage_share_by_step")
    if not isinstance(raw, dict):
        return [], previous
    events: list[tuple[str, dict[str, Any]]] = []
    memory: dict[str, Any] = dict(previous)
    for step, share in raw.items():
        if isinstance(share, bool) or not isinstance(share, (int, float)):
            continue
        attribution: str = f"{job['id']}|{step}|machine_share"
        if memory.get(attribution) == share:
            continue
        events.append((f"usage.recorded|{attribution}|{share}", {
            "provider": "claude",
            "source": "machine_share",
            "cost_kind": "observed",
            "cost_usd": None,
            "currency": None,
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "aggregation": "share_snapshot",
            "attribution_key": attribution,
            "usage_share_percent": float(share),
            "plan_points": None,
            "delta_cost_usd": None,
            "price_table_version": None,
            "models": [],
            "unpriced_models": [],
        }))
        memory[attribution] = share
    return events, memory


def _unavailable(provider: str, source: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "source": source,
        "cost_kind": "unavailable",
        "cost_usd": None,
        "delta_cost_usd": None,
        "currency": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "delta_input_tokens": None,
        "delta_output_tokens": None,
        "models": [],
        "unpriced_models": [],
        "price_table_version": None,
        "aggregation": "none",
        "attribution_key": None,
        "plan_points": None,
        "usage_share_percent": None,
    }


def _once(
    previous: dict[str, Any], key: str, payload: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    if previous.get(key) == "recorded":
        return []
    previous[key] = "recorded"
    return [(f"usage.recorded|{key}", payload)]


def _blank() -> dict[str, Any]:
    return {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None,
        "cost_usd": None,
        "models": set(),
        "unpriced_models": set(),
        "priced": False,
    }


def _add_row(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    for key in _TOKEN_KEYS:
        value: object = row.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        current: object = bucket[key]
        base: int = current if isinstance(current, int) else 0
        bucket[key] = base + value
    model: object = row.get("model")
    cost: object = row.get("cost_usd")
    priced: bool = bool(row.get("priced")) and isinstance(cost, (int, float)) and not isinstance(cost, bool)
    if priced:
        bucket["priced"] = True
        current: float = float(bucket["cost_usd"] or 0.0)
        bucket["cost_usd"] = current + float(row["cost_usd"])
        if isinstance(model, str) and model:
            bucket["models"].add(model)
    elif isinstance(model, str) and model:
        bucket["unpriced_models"].add(model)


def _totals(bucket: dict[str, Any]) -> dict[str, Any]:
    cost: object = bucket["cost_usd"]
    return {
        "input_tokens": bucket["input_tokens"],
        "output_tokens": bucket["output_tokens"],
        "cache_read_input_tokens": bucket["cache_read_input_tokens"],
        "cache_creation_input_tokens": bucket["cache_creation_input_tokens"],
        "cost_usd": None if cost is None else round(float(cost), 6),
        "models": sorted(bucket["models"]),
        "unpriced_models": sorted(bucket["unpriced_models"]),
    }


def _cumulative(
    provider: str,
    source: str,
    attribution: str,
    bucket: dict[str, Any],
    previous: object,
    price_version: str | None,
) -> dict[str, Any] | None:
    totals: dict[str, Any] = _totals(bucket)
    if previous == totals:
        return None
    old: dict[str, Any] = previous if isinstance(previous, dict) else {}
    cost: object = totals["cost_usd"]
    return {
        "provider": provider,
        "source": source,
        "cost_kind": "estimated" if bucket["priced"] else "unavailable",
        "cost_usd": cost,
        "delta_cost_usd": _delta_num(cost, old.get("cost_usd")),
        "currency": "USD" if cost is not None else None,
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cache_read_input_tokens": totals["cache_read_input_tokens"],
        "cache_creation_input_tokens": totals["cache_creation_input_tokens"],
        "delta_input_tokens": _delta_int(totals["input_tokens"], old.get("input_tokens")),
        "delta_output_tokens": _delta_int(totals["output_tokens"], old.get("output_tokens")),
        "models": totals["models"],
        "unpriced_models": totals["unpriced_models"],
        "price_table_version": price_version if bucket["priced"] else None,
        "aggregation": "cumulative_snapshot",
        "attribution_key": attribution,
        "plan_points": None,
        "usage_share_percent": None,
    }


def _delta_num(new: object, old: object) -> float | None:
    if not isinstance(new, (int, float)) or isinstance(new, bool):
        return None
    if not isinstance(old, (int, float)) or isinstance(old, bool):
        return float(new)
    return float(new) - float(old)


def _delta_int(new: object, old: object) -> int | None:
    if not isinstance(new, int) or isinstance(new, bool):
        return None
    if not isinstance(old, int) or isinstance(old, bool):
        return new
    return new - old


def _step(job: dict[str, Any]) -> str:
    step: object = job.get("step")
    return step if isinstance(step, str) and step else "none"


def _cursor_rows(repo: Path, job: dict[str, Any], job_dir: Path) -> list[dict[str, Any]] | None:
    """Token rows from Cursor transcripts. Text, tool arguments, and outputs are not kept."""
    needle: str = f"Job folder: {job_dir}/"
    rows: list[dict[str, Any]] = []
    try:
        roots: list[Path] = _cursor_roots(repo, job)
        if not any(root.is_dir() for root in roots):
            return []
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.glob("agent-transcripts/**/*.jsonl"):
                rows.extend(_cursor_file(path, needle))
    except (OSError, ValueError, TypeError):
        return None
    return rows


def _cursor_file(path: Path, needle: str) -> list[dict[str, Any]]:
    try:
        text: str = path.read_text(errors="replace")
    except OSError:
        return []
    if needle not in text:
        return []
    step_match: re.Match[str] | None = re.search(r"Step: ([a-z0-9-]+)\.", text)
    step: str = step_match.group(1) if step_match else "other"
    found: dict[str, dict[str, Any]] = {}
    for index, line in enumerate(text.splitlines()):
        if '"usage"' not in line:
            continue
        try:
            decoded: object = json.loads(line)
        except ValueError:
            continue
        if not isinstance(decoded, dict):
            continue
        message: object = decoded.get("message")
        if not isinstance(message, dict):
            continue
        usage: object = message.get("usage")
        if not isinstance(usage, dict):
            continue
        message_id: object = message.get("id")
        key: str = message_id if isinstance(message_id, str) and message_id else str(index)
        model: object = message.get("model")
        found[key] = {
            "step": step,
            "model": model if isinstance(model, str) else "",
            "priced": False,
            "cost_usd": None,
            "input_tokens": _count(usage, "input_tokens"),
            "output_tokens": _count(usage, "output_tokens"),
            "cache_read_input_tokens": _count(usage, "cache_read_input_tokens"),
            "cache_creation_input_tokens": _count(usage, "cache_creation_input_tokens"),
        }
    return list(found.values())


def _count(usage: dict[str, Any], key: str) -> int | None:
    if key not in usage:
        return None
    value: object = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _cursor_roots(repo: Path, job: dict[str, Any]) -> list[Path]:
    base: Path = Path(os.environ.get("CURSOR_CONFIG_DIR", Path.home() / ".cursor")) / "projects"
    candidates: list[Path] = [repo]
    worktree: object = job.get("worktree")
    if isinstance(worktree, str) and worktree.strip():
        candidates.append(Path(worktree))
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        slug: str = re.sub(r"[^A-Za-z0-9]", "-", str(candidate)).strip("-")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        roots.append(base / slug)
    return roots
