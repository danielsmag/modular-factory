"""What each agent did, from Claude Code and Cursor subagent transcripts.

A transcript belongs to a job when its prompt contains `Job folder: <path>/`.
Claude's files are the same ones `cost.py` reads. Cursor writes one jsonl per
subagent under `~/.cursor/projects/<repo>/agent-transcripts`. Neither format is
a documented interface, so a bad file is skipped and the dashboard still answers.

A run stores the tool name, a short label, and a step's final summary. It does
not store or send tool output, file contents, or assistant replies.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config, shown_agent
from .cost import _price, transcripts_dir
from .util import parse_time, write_atomic

NOTICE: str = "Agent activity is recorded for Claude Code and Cursor."
_MAX_LINES: int = 40
_MAX_CHARS: int = 2048
_EDIT_TOOLS: frozenset[str] = frozenset({
    "write", "edit", "multiedit", "notebookedit", "strreplace", "search_replace", "delete",
})
_READ_TOOLS: frozenset[str] = frozenset({"read", "grep", "glob"})
_SHELL_TOOLS: frozenset[str] = frozenset({"bash", "shell"})
_STEP_TOOLS: frozenset[str] = frozenset({"updatecurrentstep", "update_current_step"})
_STEP: re.Pattern[str] = re.compile(r"Step: ([a-z0-9-]+)\.")
_AGENT: re.Pattern[str] = re.compile(r"Agent: (\S+?)\.")
_ROUND: re.Pattern[str] = re.compile(r"Round: (\d+)\.")
_ATTEMPT: re.Pattern[str] = re.compile(r"Attempt: (\d+)\.")
_REVIEWER: re.Pattern[str] = re.compile(r"You are the ([a-z0-9-]+) reviewer")
_ROUND_ATTEMPT: re.Pattern[str] = re.compile(r"round (\d+), attempt (\d+)", re.IGNORECASE)
_REVIEW_ROUND: re.Pattern[str] = re.compile(r"Review round (\d+)")
_FINAL_ROUND: re.Pattern[str] = re.compile(r"Final round: (\d+)")
_FOLDER: re.Pattern[str] = re.compile(r"Job folder: (.+?)/\.(?:\s|$)")
_CURSOR_STAMP: re.Pattern[str] = re.compile(r"<timestamp>(.*?)</timestamp>", re.DOTALL)
_CURSOR_WHEN: re.Pattern[str] = re.compile(
    r"^(?:[A-Za-z]+,\s+)?"
    r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s*[AP]M)"
    r"(?:\s*\(UTC(?:([+-])(\d{1,2})(?::?(\d{2}))?)?\))?\s*$",
    re.IGNORECASE,
)
_PRIVATE_KEY: re.Pattern[str] = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_PRIVATE_KEY, "[redacted]"),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1[redacted]"),
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer [redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "[redacted]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"), "[redacted]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}\b"), "[redacted]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[redacted]"),
)


@dataclass
class CachedTranscript:
    size: int
    mtime_ns: int
    growing: bool
    error: bool
    folder: str | None
    runs: list[dict[str, Any]]


class TranscriptCache:
    """Parsed transcripts, reused until the file's size or mtime changes."""

    def __init__(self) -> None:
        self._files: dict[str, CachedTranscript] = {}
        self._lock: threading.Lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._files.clear()

    def load(self, path: Path, prices: dict[str, dict[str, float]]) -> CachedTranscript:
        with self._lock:
            stat: Any = path.stat()
            key: str = str(path)
            size: int = stat.st_size
            mtime_ns: int = stat.st_mtime_ns
            previous: CachedTranscript | None = self._files.get(key)
            if previous is not None and previous.size == size and previous.mtime_ns == mtime_ns:
                if previous.error or not previous.growing:
                    return previous
                settled: CachedTranscript = CachedTranscript(
                    size, mtime_ns, False, False, previous.folder, previous.runs,
                )
                self._files[key] = settled
                return settled
            try:
                folder: str | None
                runs: list[dict[str, Any]]
                folder, runs = _parse(path, prices)
                loaded: CachedTranscript = CachedTranscript(size, mtime_ns, True, False, folder, runs)
            except (OSError, ValueError, TypeError, KeyError):
                loaded = CachedTranscript(size, mtime_ns, False, True, None, [])
            self._files[key] = loaded
            return loaded


_CACHE: TranscriptCache = TranscriptCache()


def clear_cache() -> None:
    _CACHE.clear()


def job_activity(cfg: Config, job: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    """Runs for one job: live transcripts over the snapshot, plus a waiting row."""
    return ActivityLog(cfg, _CACHE).payload(job, Path(job_dir))


def write_snapshot(cfg: Config, job: dict[str, Any], job_dir: Path) -> None:
    """Store finished runs in the job folder. A waiting row for the next step is left out."""
    try:
        payload: dict[str, Any] = job_activity(cfg, job, job_dir)
        runs: list[dict[str, Any]] = []
        raw_runs: object = payload.get("runs")
        if isinstance(raw_runs, list):
            for run in raw_runs:
                if not isinstance(run, dict):
                    continue
                events: object = run.get("events")
                waiting: bool = run.get("status") == "running" and (not isinstance(events, list) or not events)
                if not waiting:
                    runs.append(run)
        stored: dict[str, Any] = {"runs": runs}
        error: object = payload.get("error")
        if isinstance(error, str) and error:
            stored["error"] = error
        write_atomic(Path(job_dir) / "activity.json", json.dumps(stored, indent=2) + "\n")
    except (OSError, ValueError, TypeError):
        return


class ActivityLog:
    def __init__(self, cfg: Config, cache: TranscriptCache) -> None:
        self._cfg: Config = cfg
        self._cache: TranscriptCache = cache

    def payload(self, job: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        snapshot: list[dict[str, Any]]
        snap_error: str | None
        snapshot, snap_error = _read_snapshot(job_dir)
        claude: bool = "claude" in self._cfg.tools
        cursor: bool = "cursor" in self._cfg.tools
        supported: bool = claude or cursor
        errors: list[str] = []
        if snap_error:
            errors.append(snap_error)
        live: list[tuple[dict[str, Any], bool]] = []
        if supported:
            found: list[tuple[dict[str, Any], bool]]
            count: int
            found, count = self._live(job, job_dir, claude, cursor)
            live = found
            if count:
                word: str = "transcript" if count == 1 else "transcripts"
                errors.append(f"{count} {word} unreadable")
        runs: list[dict[str, Any]] = _merge(snapshot, live, job) if supported else _finished(snapshot)
        if supported:
            runs.extend(self._placeholders(job, runs))
        runs.sort(key=_sort_key)
        _annotate(self._cfg, runs)
        body: dict[str, Any] = {"runs": runs}
        if errors:
            body["error"] = "; ".join(errors)
        if not runs and not supported:
            body["notice"] = NOTICE
        return body

    def _live(
        self, job: dict[str, Any], job_dir: Path, claude: bool, cursor: bool,
    ) -> tuple[list[tuple[dict[str, Any], bool]], int]:
        found: list[tuple[dict[str, Any], bool]] = []
        errors: int = 0
        prices: dict[str, dict[str, float]] = _prices_of(self._cfg)
        created: datetime | None = _created(job)
        roots: list[tuple[Path, str]] = []
        if claude:
            roots.append((transcripts_dir(self._cfg.repo), "*/subagents/agent-*.jsonl"))
        if cursor:
            for cursor_root in _cursor_roots(self._cfg.repo, job):
                roots.append((cursor_root, "agent-transcripts/*/subagents/*.jsonl"))
        for root, pattern in roots:
            if not root.is_dir():
                continue
            try:
                paths: list[Path] = sorted(root.glob(pattern))
            except OSError:
                errors += 1
                continue
            for path in paths:
                cached: CachedTranscript = self._cache.load(path, prices)
                if cached.error:
                    errors += 1
                    continue
                if not cached.runs or cached.folder is None or not _same_dir(cached.folder, job_dir):
                    continue
                for run in cached.runs:
                    if _before(run, created):
                        continue
                    found.append((run, cached.growing and run is cached.runs[-1]))
        return found, errors

    def _placeholders(self, job: dict[str, Any], runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        step_name: object = job.get("step")
        if not isinstance(step_name, str):
            return []
        step: object = self._cfg.pipeline_of(job).steps.get(step_name)
        if not isinstance(step, dict):
            return []
        kind: object = step.get("type")
        if kind not in {"agent", "review", "verify"}:
            return []
        round_n: object = job.get("round")
        attempt_n: object = job.get("attempt")
        if isinstance(round_n, bool) or not isinstance(round_n, int):
            return []
        if isinstance(attempt_n, bool) or not isinstance(attempt_n, int):
            return []
        missing: list[dict[str, Any]] = []
        for agent in _expected(self._cfg, job, step_name, kind):
            if _covered(runs, agent, step_name, round_n, attempt_n, str(kind)):
                continue
            missing.append({
                "agent": agent, "step": step_name, "round": round_n, "attempt": attempt_n,
                "status": "running", "events": [],
            })
        return missing


def _expected(cfg: Config, job: dict[str, Any], step_name: str, kind: object) -> list[str]:
    if kind in {"agent", "verify"}:
        risk: str | None = None
        intake: object = job.get("intake")
        if isinstance(intake, dict) and isinstance(intake.get("risk"), str):
            risk = intake["risk"]
        return [shown_agent(cfg.agent_name(step_name, risk))]
    if kind != "review":
        return []
    agents: object = job.get("agents")
    reviewers: object = agents.get("reviewers") if isinstance(agents, dict) else None
    if not isinstance(reviewers, dict):
        return []
    names: list[str] = []
    for value in reviewers.values():
        if isinstance(value, str):
            names.append(shown_agent(value))
    return names


def _covered(runs: list[dict[str, Any]], agent: str, step: str, round_n: int, attempt_n: int, kind: str) -> bool:
    for run in runs:
        if run.get("round") != round_n or run.get("attempt") != attempt_n:
            continue
        if kind == "review":
            if run.get("agent") == agent and run.get("step") == step:
                return True
        elif run.get("step") == step:
            return True
    return False


def _finished(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    done: list[dict[str, Any]] = []
    for run in runs:
        copy: dict[str, Any] = dict(run)
        copy["status"] = "done"
        done.append(copy)
    return done


def _merge(
    snapshot: list[dict[str, Any]], live: list[tuple[dict[str, Any], bool]], job: dict[str, Any],
) -> list[dict[str, Any]]:
    presented: list[dict[str, Any]] = [_present(run, growing, job) for run, growing in live]
    live_keys: set[tuple[Any, ...]] = {_key(run) for run in presented}
    chosen: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for run in snapshot:
        key: tuple[Any, ...] = _key(run)
        if key in live_keys or key in seen:
            continue
        seen.add(key)
        stored: dict[str, Any] = dict(run)
        stored["status"] = "done"
        chosen.append(stored)
    for run in presented:
        key = _key(run)
        if key in seen:
            continue
        seen.add(key)
        chosen.append(run)
    return chosen


def _present(run: dict[str, Any], _growing: bool, job: dict[str, Any]) -> dict[str, Any]:
    shown: dict[str, Any] = {key: value for key, value in run.items() if key != "events"}
    shown["events"] = _public_events(run.get("events"))
    current: bool = _current(shown, job)
    shown["status"] = "running" if current else "done"
    if current:
        shown.pop("ended_at", None)
    shown["duration_s"] = _duration(shown.get("started_at"), None if current else shown.get("ended_at"), current)
    return shown


def _current(run: dict[str, Any], job: dict[str, Any]) -> bool:
    return (
        run.get("step") == job.get("step")
        and run.get("round") == job.get("round")
        and run.get("attempt") == job.get("attempt")
        and run.get("round") is not None
        and run.get("attempt") is not None
    )


def _duration(started: object, ended: object, running: bool) -> int | None:
    if not isinstance(started, str):
        return None
    try:
        start: datetime = parse_time(started)
    except ValueError:
        return None
    finish: datetime
    if running:
        finish = datetime.now(UTC)
    elif isinstance(ended, str):
        try:
            finish = parse_time(ended)
        except ValueError:
            return None
    else:
        return None
    return max(0, int((finish - start).total_seconds()))


def _key(run: dict[str, Any]) -> tuple[Any, ...]:
    return (run.get("agent"), run.get("step"), run.get("round"), run.get("attempt"), run.get("started_at"))


def _sort_key(run: dict[str, Any]) -> tuple[str, str]:
    started: object = run.get("started_at")
    when: str = started if isinstance(started, str) else "9999-99-99T99:99:99Z"
    agent: object = run.get("agent")
    name: str = agent if isinstance(agent, str) else ""
    return when, name


def _cursor_roots(repo: Path, job: dict[str, Any]) -> list[Path]:
    base: Path = Path(os.environ.get("CURSOR_CONFIG_DIR", Path.home() / ".cursor")) / "projects"
    candidates: list[Path] = [Path(repo)]
    worktree: object = job.get("worktree")
    if isinstance(worktree, str) and worktree.strip():
        candidates.append(Path(worktree))
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        slug: str = _cursor_slug(candidate)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        roots.append(base / slug)
    return roots


def _cursor_slug(path: Path) -> str:
    try:
        text: str = str(path.resolve())
    except OSError:
        text = str(path)
    return re.sub(r"[^A-Za-z0-9]", "-", text).strip("-")


def _created(job: dict[str, Any]) -> datetime | None:
    stamp: object = job.get("created_at")
    if not isinstance(stamp, str):
        return None
    try:
        return parse_time(stamp)
    except ValueError:
        return None


def _before(run: dict[str, Any], created: datetime | None) -> bool:
    """A run from an earlier job that had the same folder. `created_at` has whole seconds, so compare on those."""
    started: object = run.get("started_at")
    if created is None or not isinstance(started, str):
        return False
    try:
        return parse_time(started).replace(microsecond=0) < created
    except (ValueError, TypeError):
        return False


def _same_dir(folder: str, job_dir: Path) -> bool:
    found: Path = Path(folder)
    wanted: Path = Path(job_dir)
    if found == wanted:
        return True
    try:
        return found.resolve() == wanted.resolve()
    except OSError:
        return False


def _read_snapshot(job_dir: Path) -> tuple[list[dict[str, Any]], str | None]:
    path: Path = job_dir / "activity.json"
    if not path.is_file():
        return [], None
    try:
        loaded: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], "activity.json unreadable"
    if not isinstance(loaded, dict):
        return [], "activity.json unreadable"
    runs: object = loaded.get("runs")
    if not isinstance(runs, list):
        return [], "activity.json unreadable"
    clean: list[dict[str, Any]] = []
    for item in runs:
        if not isinstance(item, dict):
            continue
        stored: dict[str, Any] = {key: value for key, value in item.items() if key != "result"}
        stored["events"] = _public_events(item.get("events"))
        clean.append(stored)
    return clean, None


def _parse(path: Path, prices: dict[str, dict[str, float]]) -> tuple[str | None, list[dict[str, Any]]]:
    """The job folder and one run per factory prompt: a resumed builder gets a run for each round."""
    records: list[dict[str, Any]] = _records(path)
    modified: str = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    folder: str | None = None
    runs: list[dict[str, Any]] = []
    segments: list[list[dict[str, Any]]] = _segments(records)
    for index, segment in enumerate(segments):
        found: tuple[str, dict[str, Any]] | None = _parse_segment(
            segment, prices, modified, last=index == len(segments) - 1)
        if found is None:
            continue
        if folder is None:
            folder = found[0]
        if found[0] == folder:
            runs.append(found[1])
    return folder, runs


def _segments(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a transcript before each later factory prompt, which is how a resumed agent gets a new round."""
    out: list[list[dict[str, Any]]] = [[]]
    for record in records:
        if out[-1] and _prompt_text(out[-1]) and _factory_prompt(record):
            out.append([])
        out[-1].append(record)
    return out


def _factory_prompt(record: dict[str, Any]) -> bool:
    if record.get("type") != "user":
        return False
    text: str = _message_text(record).lstrip()
    return text.startswith("Job: ") and "Job folder:" in text


def _parse_segment(
    records: list[dict[str, Any]], prices: dict[str, dict[str, float]], modified: str, last: bool,
) -> tuple[str, dict[str, Any]] | None:
    prompt: str = _prompt_text(records)
    if "Job folder:" not in prompt:
        return None
    identity: dict[str, Any] = _identity(prompt)
    folder: object = identity.pop("folder")
    if not isinstance(folder, str):
        return None
    job_dir: Path = Path(folder)
    events: list[dict[str, Any]] = []
    usage_by_message: dict[str, tuple[str, dict[str, Any]]] = {}
    open_tools: list[dict[str, Any]] = []
    stamps: list[str] = []
    for index, record in enumerate(records):
        stamp: str | None = _stamp(record)
        if stamp:
            stamps.append(stamp)
        message: object = record.get("message")
        if not isinstance(message, dict):
            continue
        if record.get("type") == "assistant":
            _remember_usage(usage_by_message, message, str(index))
            _assistant_blocks(message.get("content"), job_dir, stamp, events, open_tools)
        else:
            _attach_results(message.get("content"), open_tools)
    result: str = _finish_result(events)
    run: dict[str, Any] = dict(identity)
    run["events"] = events
    if result:
        run["result"] = result
    if stamps:
        run["started_at"] = stamps[0]
        later: bool = last and len(stamps) == 1 and modified > stamps[0]
        run["ended_at"] = modified if later else stamps[-1]
    else:
        run["started_at"] = modified
        run["ended_at"] = modified
    _add_cost(run, usage_by_message, prices)
    return folder, run


def _records(path: Path) -> list[dict[str, Any]]:
    raw: str = path.read_text(encoding="utf-8", errors="replace")
    rows: list[str] = raw.splitlines()
    if raw and not raw.endswith("\n") and rows:
        try:
            json.loads(rows[-1])
        except json.JSONDecodeError:
            rows = rows[:-1]
    records: list[dict[str, Any]] = []
    for row in rows:
        if not row.strip():
            continue
        loaded: object = json.loads(row)
        if not isinstance(loaded, dict):
            raise ValueError("transcript line is not an object")
        records.append(_normalize_record(loaded))
    return records


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Claude records already carry `type`. Cursor records carry `role` and a display timestamp."""
    if record.get("type") in {"assistant", "user"}:
        return record
    role: object = record.get("role")
    if role not in {"assistant", "user"}:
        return record
    message: object = record.get("message")
    normalized: dict[str, Any] = {"type": role, "message": message if isinstance(message, dict) else {}}
    stamp: object = record.get("timestamp")
    if isinstance(stamp, str) and stamp:
        normalized["timestamp"] = stamp
        return normalized
    if role == "user" and isinstance(message, dict):
        found: re.Match[str] | None = _CURSOR_STAMP.search(_content_text(message.get("content")))
        if found:
            iso: str | None = _cursor_iso(found.group(1).strip())
            if iso:
                normalized["timestamp"] = iso
    return normalized


def _cursor_iso(raw: str) -> str | None:
    match: re.Match[str] | None = _CURSOR_WHEN.match(raw.strip())
    if match is None:
        return None
    try:
        local: datetime = datetime.strptime(match.group(1), "%b %d, %Y, %I:%M %p")
    except ValueError:
        return None
    sign: str | None = match.group(2)
    if sign is None:
        aware: datetime = local.replace(tzinfo=UTC)
    else:
        hours: int = int(match.group(3) or 0)
        minutes: int = int(match.group(4) or 0)
        direction: int = 1 if sign == "+" else -1
        offset: timezone = timezone(timedelta(hours=direction * hours, minutes=direction * minutes))
        aware = local.replace(tzinfo=offset).astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def _prompt_text(records: list[dict[str, Any]]) -> str:
    for record in records:
        text: str = _message_text(record)
        if "Job folder:" in text:
            return text
    return ""


def _message_text(record: dict[str, Any]) -> str:
    message: object = record.get("message")
    if not isinstance(message, dict):
        return ""
    return _content_text(message.get("content"))


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return " ".join(parts)


def _identity(prompt: str) -> dict[str, Any]:
    step_match: re.Match[str] | None = _STEP.search(prompt)
    step: str = step_match.group(1) if step_match else "other"
    agent_match: re.Match[str] | None = _AGENT.search(prompt)
    reviewer_match: re.Match[str] | None = _REVIEWER.search(prompt)
    if agent_match:
        agent: str = agent_match.group(1)
    elif reviewer_match:
        agent = f"mfactory-reviewer-{reviewer_match.group(1)}"
    else:
        agent = f"mfactory-{step}"
    round_n: int | None = None
    attempt_n: int | None = None
    round_match: re.Match[str] | None = _ROUND.search(prompt)
    attempt_match: re.Match[str] | None = _ATTEMPT.search(prompt)
    if round_match:
        round_n = int(round_match.group(1))
    if attempt_match:
        attempt_n = int(attempt_match.group(1))
    if round_n is None or attempt_n is None:
        pair: re.Match[str] | None = _ROUND_ATTEMPT.search(prompt)
        if pair:
            if round_n is None:
                round_n = int(pair.group(1))
            if attempt_n is None:
                attempt_n = int(pair.group(2))
    if round_n is None:
        review_round: re.Match[str] | None = _REVIEW_ROUND.search(prompt)
        final_round: re.Match[str] | None = _FINAL_ROUND.search(prompt)
        if review_round:
            round_n = int(review_round.group(1))
        elif final_round:
            round_n = int(final_round.group(1))
    folder_match: re.Match[str] | None = _FOLDER.search(prompt)
    folder: str | None = folder_match.group(1) if folder_match else None
    return {"agent": agent, "step": step, "round": round_n, "attempt": attempt_n, "folder": folder}


def _stamp(record: dict[str, Any]) -> str | None:
    value: object = record.get("timestamp")
    if not isinstance(value, str) or not value:
        return None
    return value


def _remember_usage(
    usage_by_message: dict[str, tuple[str, dict[str, Any]]], message: dict[str, Any], fallback: str,
) -> None:
    usage: object = message.get("usage")
    if not isinstance(usage, dict):
        return
    model: object = message.get("model")
    model_name: str = model if isinstance(model, str) else ""
    message_id: object = message.get("id")
    key: str = message_id if isinstance(message_id, str) and message_id else fallback
    usage_by_message[key] = (model_name, usage)


def _assistant_blocks(
    content: object, job_dir: Path, stamp: str | None,
    events: list[dict[str, Any]], open_tools: list[dict[str, Any]],
) -> None:
    for block in _blocks(content):
        kind: object = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            event: dict[str, Any] = {"kind": "text", "text": _show(block["text"])}
            if stamp:
                event["at"] = stamp
            events.append(event)
        elif kind == "tool_use":
            name: object = block.get("name")
            tool_name: str = name if isinstance(name, str) and name else "tool"
            tool_event: dict[str, Any]
            secret: bool
            tool_event, secret = _tool_event(tool_name, block.get("input"), job_dir, stamp)
            events.append(tool_event)
            if tool_event.get("kind") == "step":
                continue
            tool_id: object = block.get("id")
            open_tools.append({
                "event": tool_event,
                "id": tool_id if isinstance(tool_id, str) else None,
                "secret": secret,
                "filled": secret,
            })


def _blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict):
            blocks.append(block)
    return blocks


def _tool_event(name: str, raw: object, job_dir: Path, at: str | None) -> tuple[dict[str, Any], bool]:
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    lowered: str = name.strip().lower().rsplit(".", 1)[-1]
    if lowered in _STEP_TOOLS:
        return _step_event(data, at), False
    path: str = _first(data, ("file_path", "path", "file", "notebook_path"))
    secret: bool = bool(path) and _secret_path(path)
    kind: str = "edit" if lowered in _EDIT_TOOLS else "tool"
    event: dict[str, Any] = {"kind": kind, "name": name}
    if at:
        event["at"] = at
    if lowered in _SHELL_TOOLS:
        command: str = _show(_first(data, ("command", "cmd")))
        description: str = _show(_first(data, ("description",)))
        event["summary"] = description or command
        if description and command and command != description:
            event["detail"] = command
    elif lowered in _READ_TOOLS:
        pattern: str = _first(data, ("pattern", "query", "glob", "regex"))
        bits: list[str] = [bit for bit in (path, pattern) if bit]
        event["summary"] = _show(" ".join(bits))
        if path:
            event["path"] = path
    elif kind == "edit":
        event["summary"] = path or name
        if path:
            event["path"] = path
        linked: str | None = _job_file(path, job_dir) if path and not secret else None
        if linked:
            event["job_file"] = linked
    else:
        keys: list[str] = [key for key in list(data.keys())[:8]]
        event["summary"] = ", ".join(keys)
    if secret:
        event["output"] = "[redacted]"
    return event, secret


def _first(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value: object = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _attach_results(content: object, open_tools: list[dict[str, Any]]) -> None:
    for tool_id, text in _tool_results(content):
        target: dict[str, Any] | None = _take_tool(open_tools, tool_id)
        if target is None or target["filled"]:
            continue
        event: dict[str, Any] = target["event"]
        event["output"] = "[redacted]" if target["secret"] else _show(text)
        target["filled"] = True


def _tool_results(content: object) -> list[tuple[str | None, str]]:
    if not isinstance(content, list):
        return []
    found: list[tuple[str | None, str]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_id: object = block.get("tool_use_id")
        found.append((tool_id if isinstance(tool_id, str) else None, _result_text(block.get("content"))))
    return found


def _take_tool(open_tools: list[dict[str, Any]], tool_id: str | None) -> dict[str, Any] | None:
    if tool_id:
        for target in open_tools:
            if target["id"] == tool_id:
                return None if target["filled"] else target
    for target in open_tools:
        if not target["filled"]:
            return target
    return None


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text: object = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _step_event(data: dict[str, Any], at: str | None) -> dict[str, Any]:
    title: str = _show(_first(data, ("current_step", "currentStep", "title"))) or "Working"
    event: dict[str, Any] = {"kind": "step", "name": "step", "summary": title}
    if at:
        event["at"] = at
    final: str = _show(_first(data, ("final_summary", "finalSummary")))
    if final:
        event["result"] = final
    return event


def _finish_result(events: list[dict[str, Any]]) -> str:
    """The answer is the last step's final summary. Assistant prose stays off the page."""
    event: dict[str, Any]
    for event in reversed(events):
        if event.get("kind") != "step":
            continue
        found: object = event.get("result")
        if isinstance(found, str) and found:
            del event["result"]
            return found
        return ""
    return ""


def _add_cost(
    run: dict[str, Any], usage_by_message: dict[str, tuple[str, dict[str, Any]]], prices: dict[str, dict[str, float]],
) -> None:
    if not usage_by_message:
        run["cost_usd"] = None
        return
    total: float = 0.0
    unpriced: bool = False
    model: str = ""
    incoming: int = 0
    outgoing: int = 0
    for used_model, usage in usage_by_message.values():
        model = used_model or model
        incoming += int(usage.get("input_tokens") or 0)
        outgoing += int(usage.get("output_tokens") or 0)
        price: dict[str, float] | None = prices.get(used_model)
        if price is None:
            unpriced = True
            continue
        try:
            total += _price(usage, price)
        except (KeyError, TypeError):
            unpriced = True
    if model:
        run["model"] = model
    run["input_tokens"] = incoming
    run["output_tokens"] = outgoing
    run["cost_usd"] = None if unpriced else round(total, 2)


def _prices_of(cfg: Config) -> dict[str, dict[str, float]]:
    adapters: object = cfg.adapters.get("claude") if isinstance(cfg.adapters, dict) else None
    if not isinstance(adapters, dict):
        return {}
    prices: object = adapters.get("prices")
    if not isinstance(prices, dict):
        return {}
    clean: dict[str, dict[str, float]] = {}
    for model, table in prices.items():
        if isinstance(model, str) and isinstance(table, dict):
            clean[model] = table
    return clean


def _job_file(path: str, job_dir: Path) -> str | None:
    root: Path = job_dir.resolve()
    candidate: Path = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved: Path = candidate.resolve()
    except OSError:
        return None
    if not resolved.is_relative_to(root) or resolved.suffix not in {".md", ".log"}:
        return None
    return resolved.relative_to(root).as_posix()


def _secret_path(path: str) -> bool:
    name: str = Path(path).name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if "credential" in name:
        return True
    if name in {"id_rsa", "id_dsa", "id_ed25519", "id_ecdsa"}:
        return True
    return name.endswith(".pem") or name.endswith(".key")


def redact(text: str) -> str:
    cleaned: str = text
    pattern: re.Pattern[str]
    replacement: str
    for pattern, replacement in _REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _clip(text: str) -> str:
    kept: str = text
    lines: list[str] = text.splitlines()
    if len(lines) > _MAX_LINES:
        kept = "\n".join(lines[:_MAX_LINES])
    raw: bytes = kept.encode()
    if len(raw) > _MAX_CHARS:
        kept = raw[:_MAX_CHARS].decode(errors="ignore")
    if kept == text:
        return text
    removed: int = len(text) - len(kept)
    return f"{kept}\n[{removed} characters cut]"


def _show(text: str) -> str:
    return _clip(redact(text))


def _public_events(events: object) -> list[dict[str, Any]]:
    """Tool name and short label only. Text, tool output, and file contents stay out."""
    if not isinstance(events, list):
        return []
    clean: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("kind") == "text":
            continue
        item: dict[str, Any] = {
            key: value for key, value in event.items() if key not in {"output", "path", "job_file"}
        }
        summary: object = item.get("summary")
        if isinstance(summary, str):
            item["summary"] = _show(summary)
        clean.append(item)
    return clean


_EFFORT_RISKS: tuple[str | None, ...] = (None, "low", "medium", "high")


def _effort_for(cfg: Config, agent: object) -> str | None:
    """Configured Claude Code effort for the agent this run started.

    The agent name has to select the key and risk. The job's current intake
    risk is not consulted.
    """
    if not isinstance(agent, str) or not agent:
        return None
    wanted: str = shown_agent(agent)
    best_key: str | None = None
    best_risk: str | None = None
    best_len: int = -1
    for key in cfg.agent_keys(every=True):
        if not isinstance(key, str):
            continue
        for risk in _EFFORT_RISKS:
            if shown_agent(cfg.agent_name(key, risk)) != wanted:
                continue
            if len(key) <= best_len:
                continue
            best_key = key
            best_risk = risk
            best_len = len(key)
    if best_key is None:
        return None
    chosen: object = cfg.model_for("claude", best_key, best_risk)
    if not isinstance(chosen, dict):
        return None
    effort: object = chosen.get("effort")
    if isinstance(effort, str) and effort:
        return effort
    return None


def _cost_enabled(cfg: Config) -> bool:
    """Same gate as `job_cost`: Claude Code is on, and `[tools.claude].cost` is not false."""
    if "claude" not in cfg.tools:
        return False
    tools: object = cfg["tools"]
    if not isinstance(tools, dict):
        return False
    claude: object = tools.get("claude", {})
    if not isinstance(claude, dict):
        return True
    return bool(claude.get("cost", True))


def _annotate(cfg: Config, runs: list[dict[str, Any]]) -> None:
    priced: bool = _cost_enabled(cfg)
    for run in runs:
        effort: str | None = _effort_for(cfg, run.get("agent"))
        if effort is None:
            run.pop("effort", None)
        else:
            run["effort"] = effort
        if not priced:
            run["cost_usd"] = None
