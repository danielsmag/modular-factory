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
from pathlib import Path


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


def job_cost(cfg, job, job_dir):
    """(total USD, USD by step, models with no price), or None when cost isn't available."""
    if "claude" not in cfg.tools or not cfg["tools"].get("claude", {}).get("cost", True):
        return None
    root = transcripts_dir(cfg.repo)
    if not root.is_dir():
        return None
    prices = cfg.adapters["claude"].get("prices", {})
    needle = f"Job folder: {job_dir}/"
    by_step, unpriced = {}, set()
    try:
        for path in root.glob("*/subagents/agent-*.jsonl"):
            with path.open() as f:
                first = f.readline()
                prompt = _prompt(first) if first else ""
                if needle not in prompt:
                    continue
                m = re.search(r"Step: ([a-z0-9-]+)\.", prompt)
                step = m.group(1) if m else "other"
                usage_by_message = {}
                for line in f:
                    entry = json.loads(line)
                    msg = entry.get("message")
                    if entry.get("type") == "assistant" and isinstance(msg, dict) and msg.get("usage"):
                        usage_by_message[msg["id"]] = (msg["model"], msg["usage"])
            for model, usage in usage_by_message.values():
                if model not in prices:
                    unpriced.add(model)
                    continue
                by_step[step] = by_step.get(step, 0.0) + _price(usage, prices[model])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return round(sum(by_step.values()), 2), {k: round(v, 2) for k, v in by_step.items()}, sorted(unpriced)
