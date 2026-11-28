"""Per-job cost from Claude Code's transcripts. Other tools don't expose usage, so they get none.

Claude Code writes each subagent's messages, with model and token usage, to
<config>/projects/<project>/<session>/subagents/agent-<id>.jsonl. Every factory
prompt starts with `Job: <id>. Step: <step>. Job folder: <path>/`, which ties
a transcript to its job and step across sessions. The transcript format is not
a documented interface, so a Claude Code update can break this; any error here
yields no cost rather than a failed command.
"""

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def transcripts_dir(repo):
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return home / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo))


def _prompt(line):
    content = json.loads(line)["message"]["content"]
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))


def _price(usage, p):
    split = usage.get("cache_creation") or {}
    write_1h = split.get("ephemeral_1h_input_tokens", 0)
    write_5m = usage.get("cache_creation_input_tokens", 0) - write_1h
    return (usage.get("input_tokens", 0) * p["input"] + usage.get("output_tokens", 0) * p["output"]
            + write_5m * p["cache_write_5m"] + write_1h * p["cache_write_1h"]
            + usage.get("cache_read_input_tokens", 0) * p["cache_read"]) / 1e6


def _token(usage: dict[str, Any], key: str) -> int | None:
    if key not in usage:
        return None
    value: object = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def claude_usage(cfg: Any, job: dict[str, Any], job_dir: Path) -> list[dict[str, Any]] | None:
    """Per-message Claude usage for this job, or None when transcripts cannot be read.

    An empty list means the folder was readable and no transcript belonged to the job.
    That is not zero tokens and not zero cost. Rows are cumulative: reading the same
    transcripts again returns the same rows. ``cost_usd`` is an estimate from the
    adapter price table, or None when the model has no price.
    """
    if "claude" not in cfg.tools or not cfg["tools"].get("claude", {}).get("cost", True):
        return None
    root: Path = transcripts_dir(cfg.repo)
    if not root.is_dir():
        return None
    prices: dict[str, Any] = cfg.adapters["claude"].get("prices", {})
    needle: str = f"Job folder: {job_dir}/"
    created: datetime | None = _created(job)
    rows: list[dict[str, Any]] = []
    try:
        for path in root.glob("*/subagents/agent-*.jsonl"):
            with path.open() as handle:
                first: str = handle.readline()
                prompt: str = _prompt(first) if first else ""
                if needle not in prompt:
                    continue
                matched: re.Match[str] | None = re.search(r"Step: ([a-z0-9-]+)\.", prompt)
                step: str = matched.group(1) if matched else "other"
                started: object = json.loads(first).get("timestamp")
                usage_by_message: dict[str, tuple[Any, dict[str, Any]]] = {}
                for line in handle:
                    entry: dict[str, Any] = json.loads(line)
                    started = started or entry.get("timestamp")
                    message: object = entry.get("message")
                    if entry.get("type") == "assistant" and isinstance(message, dict) and message.get("usage"):
                        usage: object = message.get("usage")
                        if isinstance(usage, dict):
                            usage_by_message[str(message["id"])] = (message.get("model"), usage)
            if _earlier(started, created):
                continue
            for model, usage in usage_by_message.values():
                priced: bool = model in prices
                rows.append({
                    "step": step,
                    "model": model if isinstance(model, str) else "",
                    "priced": priced,
                    "cost_usd": _price(usage, prices[model]) if priced else None,
                    "input_tokens": _token(usage, "input_tokens"),
                    "output_tokens": _token(usage, "output_tokens"),
                    "cache_read_input_tokens": _token(usage, "cache_read_input_tokens"),
                    "cache_creation_input_tokens": _token(usage, "cache_creation_input_tokens"),
                })
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return rows


def job_cost(cfg: Any, job: dict[str, Any], job_dir: Path) -> tuple[float, dict[str, float], list[str]] | None:
    """(total USD, USD by step, models with no price), or None when cost isn't available."""
    rows: list[dict[str, Any]] | None = claude_usage(cfg, job, job_dir)
    if rows is None:
        return None
    by_step: dict[str, float] = {}
    unpriced: set[str] = set()
    for row in rows:
        model: object = row.get("model")
        if not row.get("priced"):
            if isinstance(model, str) and model:
                unpriced.add(model)
            continue
        cost: object = row.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            continue
        step: str = str(row.get("step") or "other")
        by_step[step] = by_step.get(step, 0.0) + float(cost)
    return round(sum(by_step.values()), 2), {key: round(value, 2) for key, value in by_step.items()}, sorted(unpriced)


def _created(job):
    try:
        return datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return None


def _earlier(started, created):
    """A transcript from an earlier job that had the same folder. `created_at` has whole seconds."""
    if created is None or not isinstance(started, str):
        return False
    try:
        return datetime.fromisoformat(started.replace("Z", "+00:00")).replace(microsecond=0) < created
    except ValueError:
        return False


SHARE_DAYS = 7


def usage_share(cfg, job_dir, days=SHARE_DAYS):
    """Each step's share, in percent, of all Claude Code usage on this machine in the last `days` days.

    Like the "% of usage" rows in Claude Code's `/usage`: usage is weighted by API
    price, every project's transcripts count, and usage from other machines or
    claude.ai does not. None when cost isn't available or nothing was used.
    """
    if "claude" not in cfg.tools or not cfg["tools"].get("claude", {}).get("cost", True):
        return None
    home = transcripts_dir(cfg.repo).parent
    if not home.is_dir():
        return None
    prices = cfg.adapters["claude"].get("prices", {})
    since = time.time() - days * 86400
    cutoff = datetime.fromtimestamp(since, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    needle = f"Job folder: {job_dir}/"
    seen, total, by_step = set(), 0.0, {}
    try:
        for path in home.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            step = None
            with path.open(errors="replace") as f:
                for number, line in enumerate(f):
                    if number == 0 and path.parent.name == "subagents" and needle in line:
                        try:
                            m = re.search(r"Step: ([a-z0-9-]+)\.", _prompt(line))
                        except (ValueError, KeyError, TypeError):
                            m = None
                        step = m.group(1) if m else "other"
                    if '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    msg = entry.get("message")
                    stamp = entry.get("timestamp")
                    if entry.get("type") != "assistant" or not isinstance(msg, dict) or not msg.get("usage"):
                        continue
                    if not isinstance(stamp, str) or stamp[:19] < cutoff or msg.get("id") in seen:
                        continue
                    seen.add(msg.get("id"))
                    price = prices.get(msg.get("model"))
                    if price is None:
                        continue
                    spent = _price(msg["usage"], price)
                    total += spent
                    if step:
                        by_step[step] = by_step.get(step, 0.0) + spent
    except (OSError, KeyError, TypeError):
        return None
    if total <= 0:
        return None
    return {k: round(100 * v / total, 2) for k, v in by_step.items()}
