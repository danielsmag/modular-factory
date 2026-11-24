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
    assert "sk-live-secret" not in text and "[redacted]" in text and "other job" not in text


def test_a_cursor_transcript_is_grouped_into_steps_and_a_secret_is_redacted(world, monkeypatch, tmp_path: Path) -> None:
    activity.clear_cache()
    world.configure('[tools]\nenabled = ["cursor"]\n')
    job: str = world.create(world.issue())
    folder: Path = world.dir(job)
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(tmp_path / "cursor"))
    slug: str = re.sub(r"[^A-Za-z0-9]", "-", str(world.repo.resolve())).strip("-")
    dest: Path = tmp_path / "cursor" / "projects" / slug / "agent-transcripts" / "sess" / "subagents"
    dest.mkdir(parents=True)
    prompt: str = f"Job: {job}. Step: build. Agent: mfactory-build. Round: 0. Attempt: 0. Job folder: {folder}/. "
    wrapped: str = (
        "<timestamp>Monday, Nov 23, 2026, 6:01 PM (UTC)</timestamp>\n<user_query>\n" + prompt + "\n</user_query>"
    )
    records: list[dict[str, object]] = [
        {"role": "user", "message": {"content": [{"type": "text", "text": wrapped}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "text", "text": "I'll read the spec."},
            {"type": "tool_use", "name": "UpdateCurrentStep", "input": {"current_step": "Reading the spec"}},
            {"type": "tool_use", "name": "Read", "input": {"path": str(folder / "spec.md")}},
            {"type": "tool_use", "name": "Shell", "input": {
                "command": "echo Bearer sk-live-secret", "description": "Print a token",
            }},
            {"type": "tool_use", "name": "UpdateCurrentStep", "input": {
                "current_step": "Done", "final_summary": "Wrote the spec.",
            }},
        ]}},
    ]
    text: str = "".join(json.dumps(record) + "\n" for record in records)
    (dest / "agent.jsonl").write_text(text)
    state: dict[str, object] = {"step": "build", "round": 0, "attempt": 0, "intake": None, "agents": None}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, folder)
    dumped: str = json.dumps(payload)
    assert "sk-live-secret" not in dumped and "[redacted]" in dumped
    assert "notice" not in payload
    runs: object = payload["runs"]
    assert isinstance(runs, list) and len(runs) == 1
    run: object = runs[0]
    assert isinstance(run, dict)
    events: object = run["events"]
    assert isinstance(events, list)
    steps: list[object] = [
        event.get("summary") for event in events if isinstance(event, dict) and event.get("kind") == "step"
    ]
    assert steps == ["Reading the spec", "Done"]
    assert run["result"] == "Wrote the spec."
    assert run["started_at"] == "2026-11-23T18:01:00Z"
    shell: dict[str, object] | None = next(
        (event for event in events if isinstance(event, dict) and event.get("name") == "Shell"), None,
    )
    assert shell is not None and shell["summary"] == "Print a token" and "[redacted]" in str(shell["detail"])


def test_codex_only_says_activity_is_recorded_for_claude_and_cursor(world) -> None:
    world.configure('[tools]\nenabled = ["codex"]\n')
    job: str = world.create(world.issue())
    state: dict[str, object] = {"step": "build", "round": 0, "attempt": 0}
    payload: dict[str, object] = activity.job_activity(world.factory().cfg, state, world.dir(job))
    assert payload["runs"] == []
    assert payload["notice"] == activity.NOTICE


def test_a_job_id_that_leaves_the_folder_is_refused(world) -> None:
    job: str = world.create(world.issue())
    project: Project = Project(world.repo)
    assert job_activity_response(project, job) is not None
    assert job_activity_response(project, "..") is None
