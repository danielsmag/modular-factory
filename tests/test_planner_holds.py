"""A job, or a queued issue with no job yet, waits until another job is done, without taking a slot."""

import json
from datetime import UTC, datetime

from conftest import AUTO_MERGE
from modfactory import config, graph, runstate
from modfactory.engine import Factory
from modfactory.planner import holds


def cli(world, *args):
    world.factory().store.release()  # the CLI takes the job's lock itself
    return world.cli(*args)


def hold_on(world, held_key, blocker_key, blocker_ref="repo:#1"):
    factory = world.factory()
    return holds.hold(factory, held_key, blocker_ref, world.repo, blocker_key, "needs it first")


def test_held_job_waits_and_frees_its_slot(world):
    world.configure('[run]\nconcurrency = 1\nauthors = ["alice"]\n')
    blocker_key = world.issue("Blocker", author="bob")      # queued, but never offered as a job
    held = world.create(world.issue("Held"))
    world.play(held, "build")
    held_key = world.job(held)["issue"]
    assert hold_on(world, held_key, blocker_key, f"repo:#{blocker_key}")
    other = world.create(world.issue("Other"))
    world.play(other, "build")                            # the held job's slot is free for it
    table = {r[2]: r for r in world.queue()}
    assert table[held][0] == "wait" and table[held][4] == f"held until repo:#{blocker_key} is done"
    assert table[other][0] == "resume"
    third = world.issue("Third")
    assert [r[0] for r in world.queue() if r[1] == third] == ["hold"]   # concurrency reached
    world.factory().close_job(other)
    assert [r[0] for r in world.queue() if r[1] == third] == ["new"]
    assert {r[2]: r[0] for r in world.queue()}[held] == "wait"


def test_next_refuses_held_job(world):
    blocker = world.issue("Blocker")
    held = world.create(world.issue("Held"))
    world.play(held, "build")
    hold_on(world, world.job(held)["issue"], blocker)
    said = world.next(held)
    assert len(said) == 1 and said[0].startswith(f"held {held} #")
    assert "until repo:#1 is done" in said[0] and f"mfactory plan release {held} runs it now" in said[0]
    assert not any("start-agent" in line for line in said)


def test_fresh_issue_hold(world):
    five = world.issue("Waiting")
    blocker = world.issue("Blocker")
    message = hold_on(world, five, blocker, f"repo:#{blocker}")
    assert message == f"held #{five}: until repo:#{blocker} is done"
    table = world.queue()
    assert (("wait", five, "-", "-", f"held until repo:#{blocker} is done")) in table
    assert not [r for r in table if r[0] == "new" and r[1] == five]
    assert str(five) in json.loads((world.repo / ".factory" / "jobs" / "holds.json").read_text())


def test_hold_lifts_when_blocker_done(world):
    blocker = world.start(until="build", config_text=AUTO_MERGE)
    held = world.create(world.issue("Held"))
    hold_on(world, world.job(held)["issue"], world.job(blocker)["issue"])
    assert {r[2]: r[0] for r in world.queue()}[held] == "wait"
    world.play(blocker, "done")
    assert world.job(blocker)["step"] in graph.END_STATES
    assert {r[2]: r[0] for r in world.queue()}[held] == "resume"
    assert "held_by" not in world.job(held)


def test_hold_lifts_when_blocker_job_deleted_and_issue_gone(world):
    blocker_issue = world.issue("Blocker")
    blocker = world.create(blocker_issue)
    held = world.create(world.issue("Held"))
    hold_on(world, world.job(held)["issue"], blocker_issue)
    assert {r[2]: r[0] for r in world.queue()}[held] == "wait"
    world.factory().delete(blocker, worktree=True, branch=True, remote=False, pr=False, force=True)
    world.set_gh(lambda s: s["issues"][str(blocker_issue)].update(labels=[]))
    assert {r[2]: r[0] for r in world.queue()}[held] == "resume"


def test_blocker_in_another_project_is_not_judged_here(world):
    hold = {"until": "beta:#1", "project": "/elsewhere/beta", "key": 1, "reason": "", "since": "x"}
    assert holds.blocker_done(world.factory(), hold) is None


def test_job_status_held():
    steps = {"build": {"type": "agent"}}
    held = {"until": "r:#1", "project": "p", "key": 1, "reason": "", "since": datetime.now(UTC).isoformat()}
    assert runstate.job_status({"step": "build", "held_by": held}, steps) == "held"
    assert runstate.job_status({"step": "done", "held_by": held}, steps) == "done"


def test_release_by_job_or_issue_key_and_cli(world):
    blocker = world.issue("Blocker")
    job = world.create(world.issue("Held"))
    key = world.job(job)["issue"]
    hold_on(world, key, blocker, f"repo:#{blocker}")
    res = cli(world, "plan", "release", job)
    assert res.stdout == f"released {job}\n", res.stderr
    missed = cli(world, "plan", "release", job)
    assert missed.stdout == f"not held: {job}\n" and missed.returncode == 1
    hold_on(world, key, blocker)
    assert cli(world, "plan", "release", f"#{key}").stdout == f"released #{key}\n"
    waiting = world.issue("Fresh")
    hold_on(world, waiting, blocker)
    assert cli(world, "plan", "release", str(waiting)).stdout == f"released {waiting}\n"
    assert holds.fresh_holds(world.factory()) == {}


def test_a_released_job_queues_behind_the_job_that_took_its_slot(world):
    world.configure("[run]\nconcurrency = 1\n")
    blocker = world.issue("Blocker", author="bob")
    held = world.create(world.issue("Held"))
    world.play(held, "build")
    hold_on(world, world.job(held)["issue"], blocker)
    taker = world.create(world.issue("Taker"))
    world.play(taker, "build")
    assert holds.release(world.factory(), world.job(held)["issue"])
    table = {r[2]: r for r in world.queue()}
    assert table[taker][0] == "resume"
    assert table[held][0] == "hold" and table[held][4] == "concurrency 1 reached"
    assert world.next(taker)[0].startswith("start-agent")


def test_queue_skips_a_hold_whose_job_another_process_is_working_on(world):
    held = world.create(world.issue("Held"))
    blocker = world.issue("Blocker")
    key = world.job(held)["issue"]
    holds.hold(world.factory(), key, f"repo:#{blocker}", world.repo, blocker, "")
    world.factory().store.release()
    world.set_gh(lambda s: s["issues"][str(blocker)].update(labels=[]))    # the blocker is gone: due for release
    other = Factory(config.load(world.repo), say=lambda line: None)
    other.store.open(held)                                                  # another process holds the lock
    rows = world.queue()
    assert {r[2]: r[0] for r in rows}[held] == "wait"
    assert "held_by" in world.job(held)
    other.store.release()
    assert {r[2]: r[0] for r in world.queue()}[held] == "resume"


def test_create_adopts_a_fresh_hold(world):
    blocker = world.issue("Blocker", author="bob")
    five = world.issue("Five")
    hold_on(world, five, blocker)
    job = world.create(five)
    assert world.job(job)["held_by"]["key"] == blocker
    said = world.next(job)
    assert len(said) == 1 and said[0].startswith("held ")
    assert str(five) not in holds.fresh_holds(world.factory())


def test_queue_adopts_a_fresh_hold_once_the_issue_has_a_job(world):
    blocker = world.issue("Blocker", author="bob")
    five = world.issue("Five")
    hold_on(world, five, blocker)
    factory = world.factory()
    factory.store.release()
    job = factory.create(five)["id"]               # made without noticing the hold
    data = world.job(job)
    data.pop("held_by", None)
    (world.dir(job) / "job.json").write_text(json.dumps(data))
    holds._write(factory, {str(five): {"until": f"repo:#{blocker}", "project": str(world.repo), "key": blocker,
                                       "reason": "", "since": "x"}})
    factory.store.release()
    rows = world.queue()
    assert {r[2]: r[0] for r in rows}[job] == "wait"
    assert world.job(job)["held_by"]["key"] == blocker
    assert holds.fresh_holds(world.factory()) == {}
