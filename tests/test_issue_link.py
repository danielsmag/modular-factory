"""`[project] issue_link`: the pull request body must not close the issue on merge."""

import pytest
from modfactory.util import Refused


def test_a_ready_build_must_link_the_issue_with_refs(world):
    job = world.start(until="build")
    world.next(job)
    world.build(job)
    pr = str(world.job(job)["pr"] or next(iter(world.gh["prs"])))
    world.set_gh(lambda state: state["prs"][pr].update(body="Closes #1"))
    with pytest.raises(Refused, match="body must start with `Refs #1`"):
        world.done(job)
    world.set_gh(lambda state: state["prs"][pr].update(body="Refs #1"))
    assert world.done(job) == [f"{job} #1: build READY -> gate"]
