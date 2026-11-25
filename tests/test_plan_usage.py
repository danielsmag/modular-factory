"""Claude Code plan usage: the status line recorder, its wiring, and the figures per step."""

import json

from modfactory import config, cost, plan_usage, sync


def test_plan_usage_is_recorded_wired_and_kept_per_step(world, tmp_path):
    # The status line records the limits, then runs the one it wraps.
    status = {"model": {"display_name": "Opus"}, "rate_limits": {
        "five_hour": {"used_percentage": 10, "resets_at": 1000},
        "seven_day": {"used_percentage": 30, "resets_at": 9000}}}
    assert plan_usage.statusline(json.dumps(status), "cat >/dev/null; echo mine") == "mine"
    assert plan_usage.statusline(json.dumps(status)) == "Opus · 5h 10% · 7d 30%"
    assert plan_usage.read()["seven_day"] == {"used_percentage": 30.0, "resets_at": 9000}

    # sync wires it into settings.local.json around the user's own status line; off puts that back.
    settings = world.repo / ".claude" / "settings.local.json"
    settings.parent.mkdir(exist_ok=True)
    own = {"type": "command", "command": "~/bin/line --short"}
    settings.write_text(json.dumps({"statusLine": own}))
    assert ("status line", ".claude/settings.local.json") in sync.sync(config.load(world.repo))[0]
    assert json.loads(settings.read_text())["statusLine"]["command"].endswith(
        "/.factory/bin/mfactory statusline --then '~/bin/line --short'")
    world.configure("[tools.claude]\nplan_usage = false\n")
    sync.sync(config.load(world.repo))
    assert json.loads(settings.read_text())["statusLine"] == own
    world.configure("")

    # A step keeps the points used between its start and end. A window that reset counts from zero.
    path = plan_usage.path()
    path.write_text(json.dumps(dict(status["rate_limits"], at="2026-01-01T10:00:00Z")))
    job = world.start(until="spec")
    path.write_text(json.dumps({"at": "2026-01-01T10:20:00Z",
                                "five_hour": {"used_percentage": 2, "resets_at": 2000},
                                "seven_day": {"used_percentage": 31.25, "resets_at": 9000}}))
    world.spec(job)
    world.done(job)
    assert world.job(job)["plan_by_step"] == {"spec": {"five_hour": 2.0, "seven_day": 1.25}}

    # Each step's share of this machine's usage, priced from every project's transcripts.
    job_dir = world.dir(job)

    def message(ident, tokens):
        return json.dumps({"type": "assistant", "timestamp": "2099-01-01T00:00:00.000Z", "message": {
            "id": ident, "model": "claude-sonnet-5-5", "usage": {"input_tokens": tokens, "output_tokens": 0}}})

    build = cost.transcripts_dir(world.repo) / "s1" / "subagents" / "agent-1.jsonl"
    build.parent.mkdir(parents=True)
    first = json.dumps({"type": "user", "message": {"content": f"Job: x. Step: build. Job folder: {job_dir}/."}})
    build.write_text("\n".join([first, message("a", 300_000), message("a", 300_000)]) + "\n")
    other = tmp_path / "claude" / "projects" / "-elsewhere" / "s2.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text(message("b", 700_000) + "\n")
    assert cost.usage_share(config.load(world.repo), job_dir) == {"build": 30.0}
