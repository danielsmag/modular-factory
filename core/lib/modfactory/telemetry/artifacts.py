"""Relative artifact metadata. The file bytes are hashed and not copied."""

import hashlib
from pathlib import Path
from typing import Any

_MAX_HASH_BYTES: int = 32 * 1024 * 1024


def artifact_metadata(job_dir: Path, relative: object) -> dict[str, Any] | None:
    """Size and content hash for one job file. Absolute paths and paths outside the job are ignored."""
    if not isinstance(relative, str) or not relative or relative.startswith(("/", "\\")):
        return None
    path: Path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        return None
    target: Path = job_dir / path
    try:
        if not target.is_file() or not target.resolve().is_relative_to(job_dir.resolve()):
            return None
        size: int = target.stat().st_size
    except OSError:
        return None
    hashed: str | None = None
    if size <= _MAX_HASH_BYTES:
        try:
            hashed = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            hashed = None
    suffix: str = target.suffix.lstrip(".")
    return {
        "path": path.as_posix(),
        "artifact_type": suffix or "file",
        "size_bytes": size,
        "sha256": hashed,
    }
