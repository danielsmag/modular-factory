"""Agent activity: Claude transcripts, redaction, the snapshot, and the dashboard route."""

import json
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from modfactory import activity
from modfactory.fleet.service import DashboardServer, one_handler


def _put(repo: Path, name: str, records: list[dict[str, object]], partial: str = "") -> None:
    root: Path = Path(os.environ["CLAUDE_CONFIG_DIR"])
    safe: str = re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    folder: Path = root / "projects" / safe / "session" / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    text: str = "".join(json.dumps(record) + "\n" for record in records)
    (folder / name).write_text(text + partial)


def _user(text: str, at: str = "2026-11-23T18:01:00Z") -> dict[str, object]:
    return {"type": "user", "timestamp": at, "message": {"role": "user", "content": text}}


def _assistant(
    blocks: list[dict[str, object]], at: str, usage: dict[str, int] | None = None, model: str = "claude-sonnet-5-5",
) -> dict[str, object]:
    message: dict[str, object] = {"id": at, "model": model, "content": blocks}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "timestamp": at, "message": message}


def _run(payload: dict[str, object], agent: str) -> dict[str, object]:
    runs: object = payload["runs"]
    assert isinstance(runs, list)
    found: list[dict[str, object]] = [run for run in runs if isinstance(run, dict) and run.get("agent") == agent]
    assert found, payload
    return found[-1]


def test_a_transcript_becomes_a_redacted_run_and_another_job_is_ignored(world) -> None:
    activity.clear_cache()
    job: str = world.start(until="spec")
    folder: Path = world.dir(job)
    prompt: str = (
        f"Job: {job}. Step: build. Agent: mfactory-build. Round: 2. Attempt: 1. Job folder: {folder}/. "
    )
    other: Path = folder.parent / "005-export"
    _put(world.repo, "agent-build.jsonl", [
        _user(prompt),
        _assistant([
            {"type": "text", "text": "Checks failed on the VAT rounding."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q tests/test_vat.py"}},
        ], "2026-11-23T18:01:08Z", {"input_tokens": 100000, "output_tokens": 5000}),
        {"type": "user", "timestamp": "2026-11-23T18:01:11Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": (
                "FAILED test_vat.py\nAuthorization: Bearer sk-live-secret\n"
            )},
        ]}},
        _assistant([
            {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": str(folder / "build.md")}},
            {"type": "text", "text": "STATUS: READY"},
        ], "2026-11-23T18:04:40Z", {"input_tokens": 50000, "output_tokens": 2000}),
    ], partial='{"type":')
    _put(world.repo, "agent-other.jsonl", [
        _user(f"Job: 005-export. Step: build. Agent: mfactory-build. Round: 1. Attempt: 1. Job folder: {other}/. "),
        _assistant([{"type": "text", "text": "other job"}], "2026-11-23T18:02:00Z"),
    ])
    state: dict[str, object] = {"step": "ask-author", "round": 0, "attempt": 0, "intake": None, "agents": None}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, folder)
    assert "error" not in payload
    run: dict[str, object] = _run(payload, "mfactory-build")
    assert run["step"] == "build" and run["round"] == 2 and run["attempt"] == 1
    assert run["status"] == "done"
    assert run["started_at"] == "2026-11-23T18:01:00Z"
    assert run["ended_at"] == "2026-11-23T18:04:40Z"
    assert run["duration_s"] == 220
    assert run["model"] == "claude-sonnet-5-5"
    assert run["cost_usd"] == 0.37
    assert run["result"] == "STATUS: READY"
    events: object = run["events"]
    assert isinstance(events, list)
    assert events[0]["text"] == "Checks failed on the VAT rounding."
    assert events[1]["name"] == "Bash" and events[1]["summary"] == "pytest -q tests/test_vat.py"
    assert events[1]["output"] == "FAILED test_vat.py\nAuthorization: Bearer [redacted]\n"
    assert events[2]["kind"] == "edit" and events[2]["job_file"] == "build.md"
    dumped: str = json.dumps(payload)
    assert "sk-live-secret" not in dumped and "other job" not in dumped


def test_an_old_prompt_still_names_the_reviewer_and_the_build_attempt(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    _put(world.repo, "agent-review.jsonl", [
        _user(f"Job: {job}. Step: review. Job folder: {folder}/. You are the code reviewer. Review round 2."),
        _assistant([{"type": "text", "text": "VERDICT: PASS"}], "2026-11-23T19:00:00Z"),
    ])
    _put(world.repo, "agent-old-build.jsonl", [
        _user(f"Job: {job}. Step: build. Job folder: {folder}/. Build round 2, attempt 1."),
        _assistant([{"type": "text", "text": "STATUS: READY"}], "2026-11-23T19:05:00Z"),
    ])
    state: dict[str, object] = {"step": "ask-author", "round": 0, "attempt": 0, "intake": None, "agents": None}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, folder)
    review: dict[str, object] = _run(payload, "mfactory-reviewer-code")
    assert review["round"] == 2 and review["attempt"] is None and review["result"] == "VERDICT: PASS"
    build: dict[str, object] = _run(payload, "mfactory-build")
    assert build["round"] == 2 and build["attempt"] == 1


def test_secret_files_tokens_and_long_output_are_cut(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    lines: str = "\n".join(f"line-{index}" for index in range(41))
    pem: str = "-----BEGIN PRIVATE KEY-----\nABC\n-----END PRIVATE KEY-----"
    prompt: str = f"Job: {job}. Step: build. Agent: mfactory-build. Round: 1. Attempt: 1. Job folder: {folder}/. "
    _put(world.repo, "agent-secret.jsonl", [
        _user(prompt),
        _assistant([
            {"type": "tool_use", "id": "env", "name": "Read", "input": {
                "file_path": str(folder / ".env"), "content": "TOKEN=ghp_abcdefghijklmnopqrst",
            }},
            {"type": "tool_use", "id": "sh", "name": "Bash", "input": {"command": "echo ghp_abcdefghijklmnopqrst"}},
            {"type": "tool_use", "id": "long", "name": "Bash", "input": {"command": "yes"}},
        ], "2026-11-23T18:01:08Z", {"input_tokens": 10, "output_tokens": 4}, "claude-unknown"),
        {"type": "user", "timestamp": "2026-11-23T18:01:09Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "env", "content": "TOKEN=ghp_abcdefghijklmnopqrst\n"},
            {"type": "tool_result", "tool_use_id": "sh", "content": "ghp_abcdefghijklmnopqrst\n" + pem},
            {"type": "tool_result", "tool_use_id": "long", "content": lines},
        ]}},
        _assistant([{"type": "text", "text": "done"}], "2026-11-23T18:01:20Z", model="claude-unknown"),
    ])
    state: dict[str, object] = {"step": "ask-author", "round": 0, "attempt": 0, "intake": None, "agents": None}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, folder)
    run: dict[str, object] = _run(payload, "mfactory-build")
    assert run["cost_usd"] is None
    assert run["model"] == "claude-unknown"
    assert run["input_tokens"] == 10
    events: object = run["events"]
    assert isinstance(events, list)
    read: dict[str, object] = events[0]
    shell: dict[str, object] = events[1]
    long: dict[str, object] = events[2]
    assert read["path"].endswith(".env") and read["output"] == "[redacted]"
    assert "ghp_" not in str(read["summary"])
    assert "ghp_" not in str(shell["output"]) and "PRIVATE KEY" not in str(shell["output"])
    assert "[redacted]" in str(shell["output"])
    assert "line-39" in str(long["output"]) and "line-40" not in str(long["output"])
    assert "characters cut" in str(long["output"])
    assert "ghp_" not in json.dumps(payload)


def test_a_bad_transcript_is_skipped(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    prompt: str = f"Job: {job}. Step: spec. Agent: mfactory-spec. Round: 0. Attempt: 0. Job folder: {folder}/. "
    _put(world.repo, "agent-good.jsonl", [
        _user(prompt),
        _assistant([{"type": "text", "text": "STATUS: DONE"}], "2026-11-23T18:01:08Z"),
    ])
    _put(world.repo, "agent-bad.jsonl", [_user(prompt)])
    bad: Path = Path(os.environ["CLAUDE_CONFIG_DIR"])
    safe: str = re.sub(r"[^A-Za-z0-9]", "-", str(world.repo))
    path: Path = bad / "projects" / safe / "session" / "subagents" / "agent-bad.jsonl"
    path.write_text(path.read_text() + "{bad\n")
    state: dict[str, object] = {"step": "ask-author", "round": 0, "attempt": 0, "intake": None, "agents": None}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, folder)
    assert payload["error"] == "1 transcript unreadable"
    assert _run(payload, "mfactory-spec")["result"] == "STATUS: DONE"


def test_the_current_attempt_runs_until_the_transcript_stops_growing(world) -> None:
    activity.clear_cache()
    job: str = world.start(until="build")
    state: dict[str, object] = world.job(job)
    folder: Path = world.dir(job)
    prompt: str = (
        f"Job: {job}. Step: build. Agent: mfactory-build. Round: {state['round']}. "
        f"Attempt: {state['attempt']}. Job folder: {folder}/. "
    )
    _put(world.repo, "agent-live.jsonl", [
        _user(prompt, "2026-11-23T18:10:00Z"),
        _assistant([{"type": "text", "text": "still working"}], "2026-11-23T18:10:05Z"),
    ])
    cfg = world.factory().cfg
    first: dict[str, object] = activity.job_activity(cfg, state, folder)
    assert _run(first, "mfactory-build")["status"] == "running"
    assert "ended_at" not in _run(first, "mfactory-build")
    second: dict[str, object] = activity.job_activity(cfg, state, folder)
    assert _run(second, "mfactory-build")["status"] == "done"


def test_a_step_with_no_transcript_yet_is_a_waiting_run(world) -> None:
    activity.clear_cache()
    job: str = world.start(until="build")
    state: dict[str, object] = world.job(job)
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, world.dir(job))
    waiting: list[dict[str, object]] = [
        run for run in payload["runs"] if isinstance(run, dict) and run.get("status") == "running"
    ]
    assert len(waiting) == 1
    assert waiting[0]["agent"] == "mfactory-build"
    assert waiting[0]["round"] == 1 and waiting[0]["attempt"] == 1
    assert waiting[0]["events"] == []


def test_prompts_name_the_agent_round_and_attempt(world) -> None:
    job: str = world.start(until="review")
    lines: list[str] = world.next(job)
    prompts: list[str] = [line for line in lines if line.startswith("prompt:")]
    assert prompts
    for prompt in prompts:
        assert "Agent: mfactory-reviewer-" in prompt
        assert "Step: review." in prompt
        assert "Round: 1. Attempt: 1." in prompt
    assert any("Agent: mfactory-reviewer-code-low." in prompt for prompt in prompts)


def test_leaving_a_step_writes_the_snapshot_without_a_waiting_row(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    lines: list[str] = world.next(job)
    prompt: str = lines[1].removeprefix("prompt: ")
    _put(world.repo, "agent-intake.jsonl", [
        _user(prompt),
        _assistant([
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi"}},
        ], "2026-11-23T18:00:02Z"),
        {"type": "user", "timestamp": "2026-11-23T18:00:03Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "hi\n"},
        ]}},
        _assistant([{"type": "text", "text": "DECISION: ACCEPT"}], "2026-11-23T18:00:10Z"),
    ])
    world.intake(job)
    world.done(job)
    saved: dict[str, object] = json.loads((world.dir(job) / "activity.json").read_text())
    run: dict[str, object] = _run(saved, "mfactory-intake")
    assert run["status"] == "done" and run["result"] == "DECISION: ACCEPT"
    assert all(not (item.get("status") == "running" and not item.get("events"))
               for item in saved["runs"] if isinstance(item, dict))


def fetch(url: str) -> tuple[str, object]:
    with urllib.request.urlopen(url, timeout=10) as res:
        return res.read().decode(), res.headers


def test_the_dashboard_serves_activity_and_not_the_snapshot_file(world) -> None:
    activity.clear_cache()
    job: str = world.start(until="spec")
    folder: Path = world.dir(job)
    prompt: str = f"Job: {job}. Step: build. Agent: mfactory-build. Round: 2. Attempt: 1. Job folder: {folder}/. "
    _put(world.repo, "agent-http.jsonl", [
        _user(prompt),
        _assistant([
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": str(folder / "build.md")}},
            {"type": "text", "text": "STATUS: READY"},
        ], "2026-11-23T18:04:40Z"),
        {"type": "user", "timestamp": "2026-11-23T18:04:41Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "Authorization: Bearer sk-live-secret\n"},
        ]}},
    ])
    (folder / "activity.json").write_text(json.dumps({"runs": [{
        "agent": "mfactory-build", "step": "build", "round": 2, "attempt": 1,
        "started_at": "2026-11-23T18:01:00Z", "status": "done", "result": "STALE", "events": [],
    }]}) + "\n")
    server: DashboardServer = DashboardServer(("127.0.0.1", 0), one_handler(world.repo))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base: str = f"http://127.0.0.1:{server.server_address[1]}"
    name: str = world.repo.name
    try:
        body_text: str
        headers: object
        body_text, headers = fetch(f"{base}/projects/{name}/jobs/{job}/activity")
        assert headers["Content-Type"].startswith("application/json")
        body: dict[str, object] = json.loads(body_text)
        events: object = _run(body, "mfactory-build")["events"]
        assert isinstance(events, list)
        first: object = events[0]
        assert isinstance(first, dict)
        assert first["job_file"] == "build.md"
        assert _run(body, "mfactory-build")["result"] == "STATUS: READY"
        assert "sk-live-secret" not in body_text and "STALE" not in body_text
        for path in (
            f"/projects/{name}/jobs/{job}/activity.json",
            f"/projects/{name}/jobs/../activity",
            f"/projects/{name}/jobs/%2e%2e/activity",
            f"/projects/{name}/jobs/{job}/../../activity",
        ):
            with pytest.raises(urllib.error.HTTPError) as error:
                fetch(base + path)
            assert error.value.code == 404, path
    finally:
        server.shutdown()
        server.server_close()


def test_cursor_only_has_no_recorded_activity(world) -> None:
    activity.clear_cache()
    world.configure('[tools]\nenabled = ["cursor"]\n')
    job: str = world.create(world.issue())
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, world.job(job), world.dir(job))
    assert payload["runs"] == []
    assert payload["notice"] == activity.NOTICE
