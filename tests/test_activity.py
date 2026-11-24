"""Agent activity: a secret in this job is redacted, and a job id cannot leave the jobs folder."""

import json
import os
import re
from pathlib import Path

from modfactory import activity
from modfactory.workspace.project import Project
from modfactory.workspace.service import job_activity_response


def _put(repo: Path, name: str, records: list[dict[str, object]]) -> None:
    root: Path = Path(os.environ["CLAUDE_CONFIG_DIR"])
    safe: str = re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    folder: Path = root / "projects" / safe / "session" / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    text: str = "".join(json.dumps(record) + "\n" for record in records)
    (folder / name).write_text(text)


def _user(text: str) -> dict[str, object]:
    return {"type": "user", "timestamp": "2026-11-23T18:01:00Z", "message": {"role": "user", "content": text}}


def _text(text: str) -> dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": "2026-11-23T18:01:08Z",
        "message": {"id": text, "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": text}]},
    }


def test_a_secret_in_this_job_is_redacted_and_another_job_is_left_out(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    prompt: str = f"Job: {job}. Step: build. Agent: mfactory-build. Round: 0. Attempt: 0. Job folder: {folder}/. "
    _put(world.repo, "agent-build.jsonl", [_user(prompt), _text("Bearer sk-live-secret")])
    other: Path = folder.parent / "other"
    other_prompt: str = (
        f"Job: other. Step: build. Agent: mfactory-build. Round: 0. Attempt: 0. Job folder: {other}/. "
    )
    _put(world.repo, "agent-other.jsonl", [_user(other_prompt), _text("other job")])
    state: dict[str, object] = {"step": "build", "round": 0, "attempt": 0, "intake": None, "agents": None}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, folder)
    text: str = json.dumps(payload)
    assert "sk-live-secret" not in text and "other job" not in text


def test_a_job_id_that_leaves_the_folder_is_refused(world) -> None:
    job: str = world.create(world.issue())
    project: Project = Project(world.repo)
    assert job_activity_response(project, job) is not None
    assert job_activity_response(project, "..") is None


def _assistant(
    text: str,
    model: str = "claude-sonnet-5-5",
    usage: dict[str, int] | None = None,
    tools: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    content: list[dict[str, object]] = []
    if text:
        content.append({"type": "text", "text": text})
    if tools:
        content.extend(tools)
    message: dict[str, object] = {"id": text or model, "model": model, "content": content}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "timestamp": "2026-11-23T18:01:08Z", "message": message}


def _tool_result(tool_id: str, text: str) -> dict[str, object]:
    return {
        "type": "user",
        "timestamp": "2026-11-23T18:01:09Z",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}]},
    }


def _runs(payload: dict[str, object]) -> list[dict[str, object]]:
    raw: object = payload["runs"]
    assert isinstance(raw, list)
    found: list[dict[str, object]] = []
    item: object
    for item in raw:
        assert isinstance(item, dict)
        found.append(item)
    return found


def test_an_unchanged_transcript_stays_running_and_drops_output(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    prompt: str = f"Job: {job}. Step: build. Agent: mfactory-build. Round: 1. Attempt: 1. Job folder: {folder}/. "
    tool: dict[str, object] = {
        "type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "echo sk-live-secret123"},
    }
    _put(world.repo, "agent-live.jsonl", [
        _user(prompt),
        _assistant("ASSISTANT_SENTENCE_SHOULD_NOT_LEAK", tools=[tool]),
        _tool_result("tool-1", "TOOL_RESULT_SHOULD_NOT_LEAK"),
    ])
    state: dict[str, object] = {"step": "build", "round": 1, "attempt": 1, "intake": None, "agents": None}
    cfg: object = world.factory().cfg
    first: dict[str, object] = activity.job_activity(cfg, state, folder)
    second: dict[str, object] = activity.job_activity(cfg, state, folder)
    run: dict[str, object] = _runs(second)[0]
    events: object = run["events"]
    assert isinstance(events, list) and len(events) == 1
    event: object = events[0]
    assert isinstance(event, dict)
    assert _runs(first)[0]["status"] == "running"
    assert run["status"] == "running"
    assert event["name"] == "Bash"
    assert event["summary"] == "echo [redacted]"
    assert "output" not in event
    assert "result" not in run
    text: str = json.dumps(second)
    assert "sk-live-secret123" not in text
    assert "ASSISTANT_SENTENCE_SHOULD_NOT_LEAK" not in text
    assert "TOOL_RESULT_SHOULD_NOT_LEAK" not in text


def test_a_secret_in_a_read_path_is_not_sent(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    secret_path: str = "notes/sk-live-secret123.md"
    prompt: str = f"Job: {job}. Step: build. Agent: mfactory-build. Round: 1. Attempt: 1. Job folder: {folder}/. "
    read_tool: dict[str, object] = {
        "type": "tool_use", "id": "tool-read", "name": "Read", "input": {"file_path": secret_path},
    }
    edit_tool: dict[str, object] = {
        "type": "tool_use", "id": "tool-edit", "name": "Edit", "input": {"file_path": str(folder / secret_path)},
    }
    _put(world.repo, "agent-read.jsonl", [
        _user(prompt), _assistant("READ_PROSE_SHOULD_NOT_LEAK", tools=[read_tool, edit_tool]),
    ])
    state: dict[str, object] = {"step": "build", "round": 1, "attempt": 1, "intake": None, "agents": None}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, folder)
    text: str = json.dumps(payload)
    assert "sk-live-secret123" not in text
    run: dict[str, object] = _runs(payload)[0]
    events: object = run["events"]
    assert isinstance(events, list) and len(events) == 2
    found: dict[str, dict[str, object]] = {}
    event: object
    for event in events:
        assert isinstance(event, dict)
        name: object = event.get("name")
        assert isinstance(name, str)
        found[name] = event
    assert found["Read"]["summary"] == "notes/[redacted].md"
    assert "path" not in found["Read"]
    assert "path" not in found["Edit"]
    assert "job_file" not in found["Edit"]


def test_a_past_attempt_is_done_and_a_placeholder_has_no_start(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    prompt: str = f"Job: {job}. Step: build. Agent: mfactory-build. Round: 1. Attempt: 1. Job folder: {folder}/. "
    _put(world.repo, "agent-old.jsonl", [_user(prompt), _text("already finished")])
    state: dict[str, object] = {"step": "build", "round": 1, "attempt": 2, "intake": None, "agents": None}
    runs: list[dict[str, object]] = _runs(activity.job_activity(world.factory().cfg, state, folder))
    past: dict[str, object] | None = next((item for item in runs if item.get("attempt") == 1), None)
    current: dict[str, object] | None = next((item for item in runs if item.get("attempt") == 2), None)
    assert past is not None and past["status"] == "done"
    assert current is not None and current["status"] == "running"
    assert current["events"] == []
    assert "started_at" not in current


def test_effort_comes_from_the_configured_agent(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    build_prompt: str = f"Job: {job}. Step: build. Agent: mfactory-build. Round: 1. Attempt: 1. Job folder: {folder}/. "
    other_prompt: str = (
        f"Job: {job}. Step: build. Agent: mfactory-not-configured. Round: 1. Attempt: 1. Job folder: {folder}/. "
    )
    _put(world.repo, "agent-build.jsonl", [_user(build_prompt), _text("build")])
    _put(world.repo, "agent-other.jsonl", [_user(other_prompt), _text("other")])
    state: dict[str, object] = {"step": "build", "round": 1, "attempt": 1, "intake": None, "agents": None}
    cfg: object = world.factory().cfg
    found: dict[str, dict[str, object]] = {}
    for run in _runs(activity.job_activity(cfg, state, folder)):
        agent: object = run.get("agent")
        if isinstance(agent, str):
            found[agent] = run
    assert found["mfactory-build"]["effort"] == cfg.model_for("claude", "build")["effort"]
    assert "effort" not in found["mfactory-not-configured"]


def test_cost_is_null_when_disabled_or_unpriced(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    prompt: str = f"Job: {job}. Step: build. Agent: mfactory-build. Round: 1. Attempt: 1. Job folder: {folder}/. "
    state: dict[str, object] = {"step": "build", "round": 1, "attempt": 1, "intake": None, "agents": None}
    usage: dict[str, int] = {"input_tokens": 1_000_000, "output_tokens": 0}
    _put(world.repo, "agent-cost.jsonl", [_user(prompt), _assistant("", usage=usage)])
    priced: dict[str, object] = _runs(activity.job_activity(world.factory().cfg, state, folder))[0]
    assert priced["cost_usd"] == 2.0
    assert priced["model"] == "claude-sonnet-5-5"

    activity.clear_cache()
    _put(world.repo, "agent-cost.jsonl", [_user(prompt), _assistant("", model="claude-not-a-price", usage=usage)])
    missing: dict[str, object] = _runs(activity.job_activity(world.factory().cfg, state, folder))[0]
    assert missing["cost_usd"] is None
    assert missing["model"] == "claude-not-a-price"

    activity.clear_cache()
    world.configure("[tools.claude]\ncost = false\n")
    _put(world.repo, "agent-cost.jsonl", [_user(prompt), _assistant("priced again", usage=usage)])
    off: dict[str, object] = _runs(activity.job_activity(world.factory().cfg, state, folder))[0]
    assert off["cost_usd"] is None
    assert off["model"] == "claude-sonnet-5-5"


def test_stored_activity_drops_output_prose_and_result(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    stored: dict[str, object] = {
        "runs": [{
            "agent": "mfactory-build",
            "step": "build",
            "round": 3,
            "attempt": 1,
            "result": "RESULT_PROSE_LEAK",
            "events": [
                {"kind": "text", "text": "TEXT_PROSE_LEAK"},
                {"kind": "tool", "name": "Bash", "summary": "echo sk-live-secret123", "output": "OUTPUT_LEAK"},
            ],
        }],
    }
    (folder / "activity.json").write_text(json.dumps(stored))
    state: dict[str, object] = {"step": "review", "round": 9, "attempt": 4, "intake": None, "agents": None}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, folder)
    text: str = json.dumps(payload)
    assert "RESULT_PROSE_LEAK" not in text
    assert "TEXT_PROSE_LEAK" not in text
    assert "OUTPUT_LEAK" not in text
    assert "ghx_secretvalue" not in text
    assert "[redacted]" in text
    project: Project = Project(world.repo)
    assert job_activity_response(project, "..") is None


def test_claude_off_shows_the_notice(world) -> None:
    activity.clear_cache()
    job: str = world.create(world.issue())
    world.configure('[tools]\nenabled = ["cursor"]\n')
    state: dict[str, object] = {"step": "build", "round": 1, "attempt": 1, "intake": None, "agents": None}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, world.dir(job))
    assert payload.get("notice") == activity.NOTICE
    assert payload.get("runs") == []
