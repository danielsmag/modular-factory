"""Claude Code plan usage: the 5-hour and weekly limits a subscriber sees in `/usage`.

Claude Code hands these to a status line script as `rate_limits` (each window has
`used_percentage` and `resets_at`), and nowhere else. `mfactory statusline` is
that script. It saves the figures to `~/.mfactory/plan-usage.json` and then runs
the status line the user had before, so their footer looks the same.

`sync` points `.claude/settings.local.json` at it. The engine reads the file
when an agent step starts and ends, and keeps the difference per step in
`job.json` as `plan_by_step`. Anything that goes wrong here leaves no figures,
never a failed command or a broken status line.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .util import now, parse_time, user_dir, write_atomic

FILE_NAME: str = "plan-usage.json"
WINDOWS: tuple[str, ...] = ("five_hour", "seven_day")
COMMAND_MARK: str = "mfactory statusline"
SETTINGS: str = ".claude/settings.local.json"
_THEN_TIMEOUT: float = 10.0


def path() -> Path:
    return user_dir() / FILE_NAME


def _window(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    used: object = raw.get("used_percentage")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    out: dict[str, Any] = {"used_percentage": round(float(used), 2)}
    resets: object = raw.get("resets_at")
    if isinstance(resets, (int, float, str)) and not isinstance(resets, bool):
        out["resets_at"] = resets
    return out


def figures(status: object) -> dict[str, Any] | None:
    """The windows in a status line payload, or None when it has none (API key, other tools)."""
    if not isinstance(status, dict):
        return None
    limits: object = status.get("rate_limits")
    if not isinstance(limits, dict):
        return None
    found: dict[str, Any] = {}
    for name in WINDOWS:
        window: dict[str, Any] | None = _window(limits.get(name))
        if window is not None:
            found[name] = window
    return found or None


def record(status: object) -> dict[str, Any] | None:
    """Save the payload's windows with the time they were seen. Returns what was saved."""
    found: dict[str, Any] | None = figures(status)
    if found is None:
        return None
    snapshot: dict[str, Any] = {"at": now(), **found}
    target: Path = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(target, json.dumps(snapshot) + "\n")
    except OSError:
        return None
    return snapshot


def read() -> dict[str, Any] | None:
    """The last saved figures, or None."""
    try:
        loaded: object = json.loads(path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("at"), str):
        return None
    clean: dict[str, Any] = {"at": loaded["at"]}
    for name in WINDOWS:
        window: dict[str, Any] | None = _window(loaded.get(name))
        if window is not None:
            clean[name] = window
    return clean if len(clean) > 1 else None


def delta(before: object, after: object) -> dict[str, float] | None:
    """Percentage points each window rose between two snapshots.

    None when the status line did not refresh in between, so no figure is known.
    When a window reset in between, the usage since the reset is a lower bound.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    try:
        if parse_time(after["at"]) <= parse_time(before["at"]):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    out: dict[str, float] = {}
    for name in WINDOWS:
        old: object = before.get(name)
        new: object = after.get(name)
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        rose: float = float(new["used_percentage"])
        if old.get("resets_at") == new.get("resets_at"):
            rose -= float(old["used_percentage"])
        out[name] = round(max(0.0, rose), 2)
    return out or None


def add(total: dict[str, float] | None, more: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = dict(total or {})
    for name, value in more.items():
        out[name] = round(out.get(name, 0.0) + value, 2)
    return out


# ---- the status line command ------------------------------------------------


def _config_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def _command_in(settings: Path) -> str | None:
    try:
        loaded: object = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict):
        return None
    line: object = loaded.get("statusLine")
    if not isinstance(line, dict):
        return None
    command: object = line.get("command")
    if not isinstance(command, str) or not command.strip() or COMMAND_MARK in command:
        return None
    return command


def wrapped_command(project: Path | None) -> str | None:
    """The status line Claude Code would run without ours: the project's, then the user's."""
    places: list[Path] = []
    if project is not None:
        places.append(project / ".claude" / "settings.json")
    places.append(_config_home() / "settings.json")
    for place in places:
        found: str | None = _command_in(place)
        if found:
            return found
    return None


def _project_of(status: object) -> Path | None:
    if isinstance(status, dict):
        workspace: object = status.get("workspace")
        if isinstance(workspace, dict) and isinstance(workspace.get("project_dir"), str):
            return Path(workspace["project_dir"])
    chosen: str | None = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(chosen) if chosen else None


def _default_line(status: object, found: dict[str, Any] | None) -> str:
    parts: list[str] = []
    if isinstance(status, dict):
        model: object = status.get("model")
        if isinstance(model, dict) and isinstance(model.get("display_name"), str):
            parts.append(model["display_name"])
    if found:
        for name, label in (("five_hour", "5h"), ("seven_day", "7d")):
            window: object = found.get(name)
            if isinstance(window, dict):
                parts.append(f"{label} {round(window['used_percentage'])}%")
    return " · ".join(parts)


def statusline(raw: str, then: str | None = None) -> str:
    """Record the figures in `raw`, then return what the status line should print."""
    try:
        status: object = json.loads(raw) if raw.strip() else None
    except ValueError:
        status = None
    found: dict[str, Any] | None = None
    try:
        saved: dict[str, Any] | None = record(status)
        found = saved
    except Exception:  # noqa: BLE001 - a status line must never fail
        found = None
    command: str | None = then or wrapped_command(_project_of(status))
    if not command:
        return _default_line(status, found)
    try:
        res = subprocess.run(command, shell=True, input=raw, capture_output=True, text=True,  # noqa: S602
                             timeout=_THEN_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return _default_line(status, found)
    return res.stdout.rstrip("\n")


def main(then: str | None) -> int:
    print(statusline(sys.stdin.read(), then))
    return 0


# ---- wiring it into Claude Code ---------------------------------------------


def our_command(repo: Path, then: str | None) -> str:
    command: str = f"{shlex.quote(str(repo / '.factory' / 'bin' / 'mfactory'))} statusline"
    if then:
        command += f" --then {shlex.quote(then)}"
    return command


def _then_of(command: str) -> str | None:
    """The command ours wraps, from `... statusline --then '<command>'`."""
    try:
        words: list[str] = shlex.split(command)
    except ValueError:
        return None
    if "--then" in words:
        at: int = words.index("--then")
        if at + 1 < len(words):
            return words[at + 1]
    return None


def wired(cfg: Any) -> bool:
    """True when this project should record plan usage."""
    if "claude" not in cfg.tools:
        return False
    tools: object = cfg["tools"]
    claude: object = tools.get("claude", {}) if isinstance(tools, dict) else {}
    return not isinstance(claude, dict) or bool(claude.get("plan_usage", True))


def settings_change(repo: Path, want: bool) -> tuple[str, dict[str, Any] | None] | None:
    """(action, new settings or None to delete the file) for settings.local.json, or None when it is right.

    `want` adds our status line, otherwise it is taken out. A status line already
    in settings.local.json is kept: ours runs it with `--then`, and taking ours
    out puts it back.
    """
    file: Path = repo / SETTINGS
    current: dict[str, Any] | None = None
    if file.is_file():
        try:
            loaded: object = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # not ours to repair
        if not isinstance(loaded, dict):
            return None
        current = loaded
    data: dict[str, Any] = dict(current or {})
    line: object = data.get("statusLine")
    ours: bool = isinstance(line, dict) and COMMAND_MARK in str(line.get("command", ""))
    if want:
        then: str | None = None
        if ours:
            then = _then_of(str(line["command"]))  # type: ignore[index]
        elif isinstance(line, dict) and isinstance(line.get("command"), str):
            then = line["command"]
        wanted: dict[str, Any] = {"type": "command", "command": our_command(repo, then)}
        if isinstance(line, dict) and "padding" in line:
            wanted["padding"] = line["padding"]
        if line == wanted:
            return None
        data["statusLine"] = wanted
        return ("update" if current is not None else "create"), data
    if not ours:
        return None
    then = _then_of(str(line["command"]))  # type: ignore[index]
    if then:
        restored: dict[str, Any] = {"type": "command", "command": then}
        if "padding" in line:  # type: ignore[operator]
            restored["padding"] = line["padding"]  # type: ignore[index]
        data["statusLine"] = restored
    else:
        del data["statusLine"]
    return ("remove", None) if not data else ("update", data)


def apply(repo: Path, change: tuple[str, dict[str, Any] | None]) -> None:
    file: Path = repo / SETTINGS
    action, data = change
    if data is None:
        file.unlink(missing_ok=True)
        return
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, indent=2) + "\n")
    if action == "create":
        _keep_local(repo)


def _keep_local(repo: Path) -> None:
    """settings.local.json holds this checkout's path, so git must not track it."""
    from .install import exclude_entries  # install imports sync, which imports this

    try:
        res = subprocess.run(["git", "check-ignore", "-q", SETTINGS], cwd=repo, capture_output=True)
        if res.returncode != 0:
            exclude_entries(repo, [f"/{SETTINGS}"])
    except Exception:  # noqa: BLE001 - not a git checkout, or git failed: the file still works
        pass
