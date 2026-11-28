"""Hashes of the effective configuration. The hash cannot rebuild what was not saved."""

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .redaction import is_absolute_path, looks_secret

_DROP_KEYS: frozenset[str] = frozenset({
    "email", "token", "token_file", "token_env", "email_env", "password", "authorization",
    "api_key", "secret",
})
# Excluded from the reproducibility hash, besides secrets and absolute paths:
# telemetry settings do not change execution; home-directory skill and rule files are
# machine-specific and may contain private text, so only in-repository files are hashed.


def canonical(value: object) -> str:
    """Deterministic JSON. Key order does not change the hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path: Path) -> str | None:
    try:
        raw: bytes = path.read_bytes()
    except OSError:
        return None
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def sanitize(value: object) -> object:
    """Drop secrets and absolute paths before hashing. Other values stay."""
    if isinstance(value, dict):
        kept: dict[str, object] = {}
        for key, item in value.items():
            if str(key) in _DROP_KEYS:
                continue
            kept[str(key)] = sanitize(item)
        return kept
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        if is_absolute_path(value) or looks_secret(value):
            return None
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def git_metrics(repo: Path, worktree: str | None, base: str) -> dict[str, Any]:
    """Commit ids and diff counts. Missing git data stays null, never zero."""
    found: dict[str, Any] = {
        "base_commit": _rev_parse(repo, f"origin/{base}"),
        "head_commit": None,
        "files_changed": None,
        "insertions": None,
        "deletions": None,
    }
    tree: Path | None = Path(worktree) if isinstance(worktree, str) and worktree else None
    if tree is None or not tree.exists():
        return found
    found["head_commit"] = _rev_parse(tree, "HEAD")
    if found["base_commit"] and found["head_commit"]:
        stat: str = _git(tree, "diff", "--shortstat", f"origin/{base}...HEAD")
        counts: dict[str, int] | None = _shortstat(stat)
        if counts is not None:
            found.update(counts)
    return found


class Fingerprint:
    """What was loaded for one run. File bytes are hashed only inside the repository."""

    def __init__(
        self,
        *,
        factory_version: str,
        factory_config_hash: str,
        pipeline_hash: str,
        base_commit: str | None,
        roles: dict[str, str],
        skills_hash: str,
        rules_hash: str,
        adapter: str | None,
        adapters: list[str],
    ) -> None:
        self.factory_version: str = factory_version
        self.factory_config_hash: str = factory_config_hash
        self.pipeline_hash: str = pipeline_hash
        self.base_commit: str | None = base_commit
        self.roles: dict[str, str] = roles
        self.skills_hash: str = skills_hash
        self.rules_hash: str = rules_hash
        self.adapter: str | None = adapter
        self.adapters: list[str] = adapters

    def as_payload(self, *, snapshot_stored: bool) -> dict[str, Any]:
        return {
            "factory_version": self.factory_version,
            "factory_config_hash": self.factory_config_hash,
            "pipeline_hash": self.pipeline_hash,
            "graph_hash": self.pipeline_hash,
            "base_commit": self.base_commit,
            "roles": self.roles,
            "skills_hash": self.skills_hash,
            "rules_hash": self.rules_hash,
            "adapter": self.adapter,
            "adapters": self.adapters,
            "adapter_version": None,
            "snapshot_stored": snapshot_stored,
        }


def fingerprint_config(cfg: Any, job: dict[str, Any] | None = None) -> Fingerprint:
    """Hash the merged config and the job's pipeline. ``cfg`` is a ``Config``."""
    data: dict[str, Any] = dict(cfg.data)
    data.pop("telemetry", None)
    cleaned: object = sanitize(data)
    pipe: Any = cfg.pipeline_of(job) if job is not None else cfg
    steps: object = getattr(pipe, "steps", {})
    run: object = pipe.data.get("run", {}) if isinstance(getattr(pipe, "data", None), dict) else {}
    start: object = run.get("start") if isinstance(run, dict) else None
    escalate: object = run.get("escalate_to") if isinstance(run, dict) else None
    pipeline_hash: str = digest({
        "start": start, "escalate_to": escalate, "steps": sanitize(steps),
    })
    repo: Path = Path(cfg.repo)
    tools: list[str] = [str(name) for name in cfg.tools]
    base: str = str(cfg.base) if getattr(cfg, "base", None) else "master"
    return Fingerprint(
        factory_version=_version(),
        factory_config_hash=digest(cleaned),
        pipeline_hash=pipeline_hash,
        base_commit=_rev_parse(repo, f"origin/{base}"),
        roles=_role_hashes(repo),
        skills_hash=digest(sanitize(data.get("skills", {}))),
        rules_hash=_rules_hash(repo, data.get("rules", {})),
        adapter=tools[0] if tools else None,
        adapters=tools,
    )


def _version() -> str:
    from ..util import version
    return str(version())


def _role_hashes(repo: Path) -> dict[str, str]:
    folder: Path = repo / ".factory" / "roles"
    if not folder.is_dir():
        return {}
    found: dict[str, str] = {}
    for path in sorted(folder.glob("*.md")):
        hashed: str | None = file_digest(path)
        if hashed is not None:
            found[path.stem] = hashed
    return found


def _rules_hash(repo: Path, configured: object) -> str:
    files: dict[str, str] = {}
    for name in ("AGENTS.md", "CLAUDE.md"):
        hashed: str | None = file_digest(repo / name)
        if hashed is not None:
            files[name] = hashed
    return digest({"configured": sanitize(configured), "files": files})


def _rev_parse(cwd: Path, ref: str) -> str | None:
    if not cwd.exists():
        return None
    text: str = _git(cwd, "rev-parse", ref)
    if len(text) < 7:
        return None
    return text


def _git(cwd: Path, *args: str) -> str:
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _shortstat(text: str) -> dict[str, int] | None:
    """Counts from ``git diff --shortstat``. A missing piece of a real line is zero."""
    if not text.strip():
        return None
    files: re.Match[str] | None = re.search(r"(\d+) files? changed", text)
    insertions: re.Match[str] | None = re.search(r"(\d+) insertions?\(\+\)", text)
    deletions: re.Match[str] | None = re.search(r"(\d+) deletions?\(-\)", text)
    if files is None and insertions is None and deletions is None:
        return None
    return {
        "files_changed": int(files.group(1)) if files else 0,
        "insertions": int(insertions.group(1)) if insertions else 0,
        "deletions": int(deletions.group(1)) if deletions else 0,
    }
