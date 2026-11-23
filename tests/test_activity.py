"""Agent activity: a secret in this job is redacted, and a job id cannot leave the jobs folder."""

import json
import os
import re
from pathlib import Path

from modfactory import activity
from modfactory.fleet.project import Project
from modfactory.fleet.service import job_activity_response


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


def test_a_job_id_that_leaves_the_folder_is_refused(world) -> None:
    job: str = world.create(world.issue())
    project: Project = Project(world.repo)
    assert job_activity_response(project, job) is not None
    assert job_activity_response(project, "..") is None
