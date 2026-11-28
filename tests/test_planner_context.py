"""Context added to a running job: every later agent step reads it, unlike the one-shot handoff notes."""

import pytest
from modfactory.planner import context
from modfactory.util import Refused


def add(world, job, text="Mind the cache.", source="the planner"):
    factory = world.factory()
    name = context.add_context(factory, world.job(job)["issue"], text, source)
    factory.store.release()
    return name


def test_context_reaches_every_later_step(world):
    job = world.start(until="spec")
    assert add(world, job) == "context-1.md"
    text = (world.dir(job) / "context-1.md").read_text()
    assert text.startswith("# Context 1\n\nFrom the planner.\n\nMind the cache.")
    assert "context-1.md" in " ".join(world.next(job))
    world.spec(job)
    world.done(job)
    assert world.job(job)["step"] == "build" and world.job(job)["handoff"] == []
    said = " ".join(world.next(job))
    assert "Context added to this job: read context-1.md before you start" in said


def test_context_refuses_finished_job(world):
    job = world.start(until="spec")
    factory = world.factory()
    opened = factory.store.open(job)
    opened["step"] = "done"
    factory.store.save(opened)
    with pytest.raises(Refused):
        context.add_context(factory, opened["issue"], "late", "the planner")


def test_context_refuses_issue_without_job(world):
    with pytest.raises(Refused):
        context.add_context(world.factory(), "999", "text", "the planner")


def test_context_files_numbered(world):
    job = world.start(until="spec")
    assert [add(world, job, "one"), add(world, job, "two")] == ["context-1.md", "context-2.md"]
    said = " ".join(world.next(job))
    assert "context-1.md, context-2.md" in said
