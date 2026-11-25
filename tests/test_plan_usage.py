"""Claude Code plan usage: the status line recorder, its wiring, and the figures per step."""

import json

from modfactory import config, cost, plan_usage, sync


def snapshot(at, five, week, resets=1000):
    plan_usage.path().parent.mkdir(parents=True, exist_ok=True)
    plan_usage.path().write_text(json.dumps({
        "at": at,
        "five_hour": {"used_percentage": five, "resets_at": resets},
        "seven_day": {"used_percentage": week, "resets_at": 9000},
    }))


def test_the_status_line_records_the_limits_and_runs_the_one_it_wraps(world, tmp_path):
    status = {"model": {"display_name": "Opus"}, "workspace": {"project_dir": str(world.repo)},
              "rate_limits": {"five_hour": {"used_percentage": 23.4, "resets_at": 1767000000},
                              "seven_day": {"used_percentage": 41, "resets_at": 1767500000}}}
    raw = json.dumps(status)

    assert plan_usage.statusline(raw, "cat >/dev/null; echo mine") == "mine"
    saved = plan_usage.read()
    assert saved["five_hour"] == {"used_percentage": 23.4, "resets_at": 1767000000}
    assert saved["seven_day"]["used_percentage"] == 41

    # Without --then it runs the user's own status line, and never itself.
    user = tmp_path / "claude" / "settings.json"
    user.parent.mkdir(parents=True, exist_ok=True)
    user.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo from-user"}}))
    assert plan_usage.statusline(raw) == "from-user"
    user.write_text(json.dumps({"statusLine": {"command": "/x/.factory/bin/mfactory statusline"}}))
    assert plan_usage.statusline(raw) == "Opus · 5h 23% · 7d 41%"

    # An API-key session has no rate_limits: nothing is recorded, the line still prints.
    plan_usage.path().unlink()
    assert plan_usage.statusline(json.dumps({"model": {"display_name": "Opus"}})) == "Opus"
    assert plan_usage.read() is None
    assert plan_usage.statusline("not json") == ""


def test_sync_wires_the_status_line_and_keeps_the_one_already_there(world):
    settings = world.repo / ".claude" / "settings.local.json"
    settings.parent.mkdir(exist_ok=True)
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                    "statusLine": {"type": "command", "command": "~/bin/line --short", "padding": 1}}))
    cfg = config.load(world.repo)
    actions, _ = sync.sync(cfg)
    assert ("status line", ".claude/settings.local.json") in actions
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert data["statusLine"]["padding"] == 1
    assert data["statusLine"]["command"].endswith("/.factory/bin/mfactory statusline --then '~/bin/line --short'")
    assert sync.sync(cfg, check=True)[0] == []

    # Turning it off puts the user's own status line back.
    world.configure("[tools.claude]\nplan_usage = false\n")
    actions, _ = sync.sync(config.load(world.repo))
    assert ("status line", ".claude/settings.local.json") in actions
    assert json.loads(settings.read_text())["statusLine"] == {"type": "command", "command": "~/bin/line --short",
                                                              "padding": 1}


def test_a_new_settings_file_is_kept_out_of_git_and_uninstall_removes_it(world):
    sync.sync(config.load(world.repo))
    settings = world.repo / ".claude" / "settings.local.json"
    assert "statusline" in json.loads(settings.read_text())["statusLine"]["command"]
    assert world.cli("doctor", "--skip-checks").stdout.count("settings.local.json") == 0
    status = world.cli("uninstall")
    assert "plan usage status line" in status.stdout
    assert not settings.exists()


def test_a_step_keeps_the_plan_usage_it_used(world):
    snapshot("2026-01-01T10:00:00Z", 10.0, 30.0)
    job = world.start(until="spec")
    snapshot("2026-01-01T10:20:00Z", 13.5, 31.25)
    world.spec(job)
    world.done(job)
    assert world.job(job)["plan_by_step"]["spec"] == {"five_hour": 3.5, "seven_day": 1.25}
    assert "intake" not in world.job(job)["plan_by_step"]   # the status line didn't refresh during intake
    assert world.job(job)["plan_at_entry"]["at"] == "2026-01-01T10:20:00Z"   # the next agent step's start


def test_the_difference_handles_a_reset_and_a_status_line_that_did_not_refresh():
    before = {"at": "2026-01-01T10:00:00Z", "five_hour": {"used_percentage": 90, "resets_at": 1},
              "seven_day": {"used_percentage": 50, "resets_at": 9}}
    after = {"at": "2026-01-01T11:00:00Z", "five_hour": {"used_percentage": 4, "resets_at": 2},
             "seven_day": {"used_percentage": 52, "resets_at": 9}}
    assert plan_usage.delta(before, after) == {"five_hour": 4.0, "seven_day": 2.0}
    assert plan_usage.delta(before, before) is None
    assert plan_usage.delta(None, after) is None


def test_each_step_gets_its_share_of_this_machines_usage(world, tmp_path):
    cfg = config.load(world.repo)
    job_dir = world.repo / ".factory" / "jobs" / "001-x"
    projects = tmp_path / "claude" / "projects"
    now = "2099-01-01T00:00:00.000Z"

    def message(ident, tokens, stamp=now):
        return json.dumps({"type": "assistant", "timestamp": stamp, "message": {
            "id": ident, "model": "claude-sonnet-5-5", "usage": {"input_tokens": tokens, "output_tokens": 0}}})

    build = cost.transcripts_dir(world.repo) / "s1" / "subagents" / "agent-1.jsonl"
    build.parent.mkdir(parents=True)
    first = json.dumps({"type": "user", "message": {"content": f"Job: 001-x. Step: build. Job folder: {job_dir}/."}})
    build.write_text("\n".join([first, message("a", 300_000), message("a", 300_000)]) + "\n")  # repeated id
    other = projects / "-elsewhere" / "s2.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text("\n".join([message("b", 700_000), message("c", 9_000_000, "2001-01-01T00:00:00Z")]) + "\n")

    assert cost.usage_share(cfg, job_dir) == {"build": 30.0}
