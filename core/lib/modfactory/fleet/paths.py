"""Fleet file name, schema, and the user fleet path."""

from __future__ import annotations

from pathlib import Path

from ..util import user_dir

FILE: str = "fleet.toml"
SCHEMA: int = 1
SERVED: dict[str, str] = {".md": "text/plain", ".log": "text/plain"}
HEADER: str = (
    "# Checkouts where the factory is already installed. One job still belongs to one project.\n"
    "schema = 1\n"
)


def user_file() -> Path:
    """The fleet file for this user. `subscribe` creates it, and the directory with it."""
    return user_dir() / FILE


def toml_string(value: str) -> str:
    escaped: str = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
