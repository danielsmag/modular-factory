"""Context added to a running job: a file in the job dir that every later agent step is told to read."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import graph
from ..service.tracker import Key
from ..util import Refused
from .holds import _job_of

if TYPE_CHECKING:
    from ..engine import Factory


def add_context(factory: Factory, key: Key, text: str, source: str) -> str:
    """Write `context-<k>.md` for the job of `key`, list it in `context_files`, and return the file name."""
    job: dict[str, Any] | None = _job_of(factory, key)
    if job is None:
        raise Refused(f"{factory.tracker.ref(key)} has no job to add context to")
    if job["step"] in graph.END_STATES:
        raise Refused(f"{job['id']} is already finished ({job['step']}); there is nothing to add context to")
    opened: dict[str, Any] = factory.store.open(job["id"])
    files: list[str] = opened.setdefault("context_files", [])
    k: int = len(files) + 1
    name: str = f"context-{k}.md"
    (factory.store.path(opened["id"]) / name).write_text(f"# Context {k}\n\nFrom {source}.\n\n{text}\n")
    files.append(name)
    factory.store.save(opened)
    return name
