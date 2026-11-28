"""Where the planner keeps its files: the snapshot, the findings and the scan agent's draft."""

import json
from pathlib import Path
from typing import Any

from ..util import write_atomic


class PlannerState:
    """The planner's folder. Writes are atomic, and a missing or corrupt file reads as empty."""

    def __init__(self, root: Path) -> None:
        self.root: Path = Path(root)

    @property
    def snapshot_path(self) -> Path:
        return self.root / "snapshot.json"

    @property
    def findings_path(self) -> Path:
        return self.root / "findings.json"

    @property
    def draft_path(self) -> Path:
        return self.root / "findings-draft.json"

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(data, indent=2) + "\n")

    def read_snapshot(self) -> dict[str, Any] | None:
        try:
            found: object = json.loads(self.snapshot_path.read_text())
        except (OSError, ValueError):
            return None
        return found if isinstance(found, dict) else None

    def write_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._write(self.snapshot_path, snapshot)

    def read_findings(self) -> dict[str, Any]:
        try:
            found: object = json.loads(self.findings_path.read_text())
        except (OSError, ValueError):
            found = None
        if isinstance(found, dict) and isinstance(found.get("findings"), list):
            return found
        return {"schema": 1, "scanned_at": None, "snapshot_hash": None, "frequency": None, "alert_at": None,
                "next_id": 1, "findings": []}

    def write_findings(self, findings: dict[str, Any]) -> None:
        self._write(self.findings_path, findings)
