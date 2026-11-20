"""Start and stop the dashboard without staying in the foreground."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

from ..dashboard import RunningDashboard, alive, running, state_file
from ..util import Refused, user_dir


def dashboard_argv(args: argparse.Namespace) -> list[str]:
    script: Path = Path(sys.argv[0]).resolve()
    argv: list[str] = [sys.executable, str(script), "dashboard", "--port", str(args.port)]
    if args.no_open:
        argv.append("--no-open")
    if args.all:
        argv.append("--all")
    return argv


def start_dashboard(args: argparse.Namespace) -> int:
    current: RunningDashboard | None = running()
    if current is not None:
        raise Refused(f"dashboard already running at {current.url}. Stop it with `mfactory stop`.")
    home: Path = user_dir()
    home.mkdir(parents=True, exist_ok=True)
    log_path: Path = home / "dashboard.log"
    log: TextIO = log_path.open("w", encoding="utf-8")
    proc: subprocess.Popen[bytes] = subprocess.Popen(
        dashboard_argv(args), start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    )
    log.close()
    deadline: float = time.monotonic() + 5
    while time.monotonic() < deadline:
        found: RunningDashboard | None = running()
        if found is not None and found.pid == proc.pid:
            print(f"mfactory dashboard: {found.url}")
            return 0
        if proc.poll() is not None:
            detail: str = log_path.read_text().strip()
            if detail.startswith("mfactory: "):
                detail = detail.removeprefix("mfactory: ").strip()
            raise Refused(detail or "dashboard exited before it was ready")
        time.sleep(0.05)
    proc.kill()
    raise Refused("dashboard did not start")


def stop_dashboard() -> int:
    current: RunningDashboard | None = running()
    if current is None:
        print("dashboard is not running")
        return 0
    os.kill(current.pid, signal.SIGTERM)
    deadline: float = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not alive(current.pid):
            state_file().unlink(missing_ok=True)
            print(f"stopped {current.url}")
            return 0
        time.sleep(0.05)
    raise Refused(f"dashboard {current.pid} did not stop")
