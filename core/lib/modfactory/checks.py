"""The deterministic gate: do the project's checks pass, and are protected files untouched?"""

import fnmatch
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .util import Refused, run, tail

TAIL = 80

# Paths the factory owns. A job branch may not add, change or delete anything
# here, or it could rewrite the pipeline, the checks or the agents that judge it.
OWNED = ["factory.toml", ".factory/*",
         ".claude/agents/mfactory-*", ".cursor/agents/mfactory-*", ".codex/agents/mfactory-*",
         ".claude/skills/mfactory/*", ".cursor/skills/mfactory/*", ".agents/skills/mfactory/*",
         ".claude/agents/factory-*", ".cursor/agents/factory-*", ".codex/agents/factory-*",
         ".claude/skills/factory/*", ".cursor/skills/factory/*", ".agents/skills/factory/*"]


def protected_changes(cfg, worktree):
    """Protected files this branch touched. `*` in a pattern also matches `/`."""
    diff = run(["git", "diff", "--name-status", f"origin/{cfg.base}...HEAD"], cwd=worktree).stdout
    hits = []
    for line in diff.splitlines():
        status, *paths = line.split("\t")
        for path in paths:
            owned = any(fnmatch.fnmatch(path, p) for p in OWNED)
            extra = any(fnmatch.fnmatch(path, p) for p in cfg["paths"]["protected"])
            # Projects may add files under their own protected paths, but not change existing ones.
            if owned or (extra and not status.startswith("A")):
                hits.append(f"{status[0]} {path}")
                break
    return hits


def run_commands(commands, cwd, env=None, timeout_minutes=30):
    """Run each command in order; the first failure stops. Returns (exit code, log, seconds)."""
    start, log = time.monotonic(), []
    full_env = dict(os.environ, **(env or {}))
    for command in commands:
        try:
            res = subprocess.run(["bash", "-c", command], cwd=cwd, env=full_env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=timeout_minutes * 60)
            code, output = res.returncode, res.stdout
        except subprocess.TimeoutExpired as e:
            output = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
            code, output = 124, f"{output}\n(timed out after {timeout_minutes} minutes)"
        log.append(f"$ {command}\n{output.rstrip()}\n(exit {code})")
        if code != 0:
            return code, "\n\n".join(log) + "\n", int(time.monotonic() - start)
    return 0, "\n\n".join(log) + "\n", int(time.monotonic() - start)


def gate(cfg, job, round_dir, name="checks"):
    """Run the checks in the job's worktree and write round-N/<name>.md. Returns the verdict."""
    worktree = job["worktree"]
    commands = cfg["checks"]["commands"]
    commit = run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    protected = protected_changes(cfg, worktree)
    if commands:
        code, log, seconds = run_commands(commands, worktree, {"FACTORY_JOB": job["id"]},
                                          cfg["checks"]["timeout_minutes"])
    else:
        code, log, seconds = 1, "[checks].commands in factory.toml is empty, so nothing proves this works.\n", 0
    verdict = "PASS" if code == 0 and not protected else "FAIL"

    attempt = job.get("attempt", 1)
    for suffix in (".md", ".log"):  # keep the earlier attempts of this round
        old = round_dir / f"{name}{suffix}"
        if old.exists() and name == "checks" and attempt > 1:
            old.replace(round_dir / f"{name}-{attempt - 1}{suffix}")
    (round_dir / f"{name}.log").write_text(log)
    summary = [
        f"The checks {'passed' if code == 0 else f'failed with exit {code}'} in {seconds}s",
        "Protected paths: " + ("changed: " + ", ".join(protected) if protected else "none changed"),
        f"Commit {commit[:7]}" + (", synced with the base branch before merging" if name != "checks" else ""),
    ]
    (round_dir / f"{name}.md").write_text(
        f"GATE: {verdict}\nCOMMIT: {commit}\nATTEMPT: {attempt}\n\n"
        "## Summary\n" + "\n".join(f"- {s}" for s in summary) + "\n\n"
        "## Protected paths\n" + ("\n".join(f"- {p}" for p in protected) if protected else "None changed.")
        + f"\n\n## Commands\nExit {code} in {seconds}s. Last {TAIL} lines of {name}.log:\n\n"
        f"```\n{tail(log, TAIL)}\n```\n")
    return verdict


def hook(cfg, store):
    """Claude Code Stop hook for the build agent: run the checks once and say if they fail."""
    event = json.load(sys.stdin)
    if event.get("stop_hook_active"):
        return 0  # one nudge per stop; the gate decides
    try:
        with open(Path(event["agent_transcript_path"]).expanduser()) as f:
            content = json.loads(f.readline())["message"]["content"]
        prompt = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content)
        job = store.read(prompt.split("Job: ", 1)[1].split(".", 1)[0])
    except (KeyError, IndexError, OSError, ValueError):
        return 0  # not a factory agent
    if not cfg["checks"]["commands"] or not Path(job["worktree"]).exists():
        return 0
    code, log, _ = run_commands(cfg["checks"]["commands"], job["worktree"],
                                timeout_minutes=cfg["checks"]["timeout_minutes"])
    if code != 0:
        print(f"The project's checks fail in your worktree (exit {code}). The gate will reject this "
              f"build. Fix it, or write STATUS: BLOCKED in build.md if you can't.\n\n{tail(log, 40)}",
              file=sys.stderr)
        return 2
    return 0


def run_here(cfg):
    """`factory checks`: run the project's checks in the current directory."""
    if not cfg["checks"]["commands"]:
        raise Refused("[checks].commands in factory.toml is empty")
    code, log, seconds = run_commands(cfg["checks"]["commands"], os.getcwd(),
                                      timeout_minutes=cfg["checks"]["timeout_minutes"])
    print(log, end="")
    print(f"checks: {'PASS' if code == 0 else 'FAIL'} in {seconds}s")
    return 0 if code == 0 else 1
