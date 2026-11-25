"""Job ids never repeat, and a job shows only the agents that ran for it."""

import json
import shutil

from modfactory import activity, config, cost


def test_id_is_not_reused_after_the_job_folder_is_removed(world):
    first = world.create(world.issue(title="Add tier tags 1-5"))
    assert first == "001-add-tier-tags-1"
    world.factory().store.release()
    shutil.rmtree(world.dir(first))

    again = world.create(world.issue(title="Add tier tags 1-5"))
    assert again == "002-add-tier-tags-1"
    assert (world.repo / ".factory" / "jobs" / "last_number").read_text() == "2\n"


def test_counter_starts_from_existing_folders_on_an_older_install(world):
    jobs = world.repo / ".factory" / "jobs"
    (jobs / "005-old-job").mkdir(parents=True)
    (jobs / "005-old-job" / "job.json").write_text(json.dumps({"id": "005-old-job", "issue": 99, "step": None}))

    assert world.create(world.issue(title="Next one")) == "006-next-one"
    assert (jobs / "last_number").read_text() == "6\n"


def test_unreadable_counter_falls_back_to_existing_folders(world):
    jobs = world.repo / ".factory" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "last_number").write_text("garbage\n")

    assert world.create(world.issue(title="Fresh")) == "001-fresh"


def _transcript(world, session, job_dir, stamp, tokens):
    path = cost.transcripts_dir(world.repo) / session / "subagents" / "agent-1.jsonl"
    path.parent.mkdir(parents=True)
    prompt = {"type": "user", "timestamp": stamp,
              "message": {"content": f"Job: x. Step: intake. Job folder: {job_dir}/. Round 0, attempt 0."}}
    reply = {"type": "assistant", "timestamp": stamp, "message": {
        "id": f"m-{session}", "model": "claude-sonnet-5-5", "role": "assistant",
        "usage": {"input_tokens": tokens, "output_tokens": 0}, "content": [{"type": "text", "text": "ok"}]}}
    path.write_text(json.dumps(prompt) + "\n" + json.dumps(reply) + "\n")


def test_runs_from_before_the_job_was_created_are_left_out(world):
    job_id = world.create(world.issue(title="Add tier tags"))
    job = world.job(job_id)
    job_dir = world.dir(job_id)
    _transcript(world, "old", job_dir, "2020-01-01T00:00:00.000Z", 1_000_000)
    _transcript(world, "new", job_dir, "2099-01-01T00:00:00.000Z", 2_000_000)
    activity.clear_cache()
    cfg = config.load(world.repo)

    runs = activity.job_activity(cfg, job, job_dir)["runs"]
    assert [r["started_at"] for r in runs if r.get("started_at")] == ["2099-01-01T00:00:00.000Z"]

    with_old = cost.job_cost(cfg, dict(job, created_at="2019-01-01T00:00:00Z"), job_dir)
    only_new = cost.job_cost(cfg, job, job_dir)
    assert with_old and only_new
    assert only_new[0] < with_old[0]


def test_a_run_in_the_same_second_as_the_job_is_kept(world):
    job_id = world.create(world.issue(title="Same second"))
    job = world.job(job_id)
    job_dir = world.dir(job_id)
    _transcript(world, "same", job_dir, job["created_at"].replace("Z", ".400Z"), 10)
    activity.clear_cache()

    runs = activity.job_activity(config.load(world.repo), job, job_dir)["runs"]
    assert [r["started_at"] for r in runs if r.get("started_at")] == [job["created_at"].replace("Z", ".400Z")]
