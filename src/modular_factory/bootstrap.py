"""Console script for the PyPI install. Execs the factory tree shipped as data."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    """Replace this process with ``share/modular-factory/core/bin/mfactory``."""
    root: Path = Path(sys.prefix) / "share" / "modular-factory"
    binary: Path = root / "core" / "bin" / "mfactory"
    if not binary.is_file():
        sys.exit(f"mfactory: {binary} is missing. Reinstall modular-factory.")
    os.execv(sys.executable, [sys.executable, str(binary), *sys.argv[1:]])
