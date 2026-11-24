"""Workspace file name, schema, and the user workspace path."""

from __future__ import annotations

from pathlib import Path

from ..util import user_dir

FILE: str = "workspace.toml"
LEGACY_FILE: str = "fleet.toml"
SCHEMA: int = 1
SERVED: dict[str, str] = {".md": "text/plain", ".log": "text/plain"}
HEADER: str = (
    "# Checkouts where the factory is already installed. One job still belongs to one project.\n"
    "schema = 1\n"
)


def user_file() -> Path:
    """The workspace file for this user. `workspace add` creates it, and the directory with it."""
    return user_dir() / FILE


def migrate_user_file() -> Path | None:
    """Rename the user's `fleet.toml` to `workspace.toml` once. Returns the new path when it moved."""
    new: Path = user_file()
    old: Path = new.with_name(LEGACY_FILE)
    if new.exists() or not old.is_file():
        return None
    old.rename(new)
    return new


def toml_string(value: str) -> str:
    escaped: str = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
