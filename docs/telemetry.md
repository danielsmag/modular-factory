# Telemetry

Local, structured records of factory runs. They exist so a later tool can compare
models, reviewers, skills, and pipeline settings. This version only collects and
exports. It does not call a model, change a pipeline, or choose a step.

`job.json` stays the authority for where a job is. Telemetry is a copy of what
was observed after a save. A missing event grants nothing and spends nothing.

Collection uses the Python standard library. It stays on this machine. There is
no Hub upload, database, or dashboard in this version.

## Contents

- [What is recorded](#what-is-recorded)
- [Configuration fingerprint](#configuration-fingerprint)
- [Usage and cost](#usage-and-cost)
- [Files](#files)
- [Privacy](#privacy)
- [Commands](#commands)
- [Failure behavior](#failure-behavior)
- [Extension](#extension)

## What is recorded

Each record is one JSON object in an append-only JSONL file. The envelope is
schema version 1:

| Field | Meaning |
| --- | --- |
| `event_id` | Stable id for one logical occurrence. Emitting that occurrence again does not create a second id. |
| `event_type` | One of the types below. |
| `timestamp` | UTC time the event was written, `YYYY-MM-DDTHH:MM:SSZ`. |
| `source` | `core`, `check`, or `adapter`. |
| `project_id` | Id created once in `.factory/telemetry/project.json`. Not the repository name. |
| `job_id`, `run_id` | Job id, and the id of this CLI invocation. |
| `step_execution_id` | Stable id for one entry into a step (`step` + `entered_at`). |
| `agent_execution_id` | Set for a single dispatched agent or one reviewer. Parallel reviewers each get an id in `payload.agents`. |
| `step`, `attempt`, `round` | Where the factory was. Null when the history entry does not have the number. |
| `factory_version`, `config_hash` | Factory version and the reproducibility hash below. |
| `occurrence_key` | The logical identity. Export keeps the first record for each key. |
| `payload` | Type-specific fields. Unknown keys are dropped. Missing measurements are null, never zero. |

A reader should keep unknown fields and report an unsupported `schema_version`.

| Event | When |
| --- | --- |
| `job.created` | The job folder and `job.json` exist. |
| `job.completed` | The job reached `done`, `rejected`, or `failed`. |
| `run.started`, `run.ended` | One `mfactory` command that touched the job. |
| `config.snapshot` | First use of an effective-config hash on that job. |
| `step.entered` | The job is saved in a step. |
| `step.dispatched` | `next` printed `start-agent`. This is the instruction, not proof the agent process started. |
| `step.completed`, `step.transitioned` | A persisted history entry. |
| `step.skipped` | A disabled step the graph passed through. |
| `step.interrupted` | A new command found a run that never wrote `run.ended` (the previous process died). |
| `agent.completed` | An agent or verify step's outcome was accepted. Duration is null: the factory does not see the agent process clock. |
| `check.completed` | The gate or merge checks finished. `duration_kind` is `monotonic_process`. |
| `review.completed` | One reviewer verdict (`PASS` or `CHANGES`) on a saved review step. |
| `human.requested` | A human step is waiting. Role only (`author`, `operator`, `maintainer`, `named`, `role`). No login and no question text. |
| `human.responded` | A human step was answered. Outcome only. `has_text` is null: the words are not copied. |
| `usage.recorded` | Tokens, estimated cost, plan points, or a machine usage share. |
| `artifact.recorded` | Relative path, size, and SHA-256 of a job file such as `build.md`. Not the file body. |
| `error.occurred` | A failed check, a blocked or rejected outcome, or a loop limit. No free-text reason. |

`step.completed` durations are `wall_clock`: the difference of the saved
`entered_at` and history `at` timestamps, labeled `dispatch_to_completion`.
They are not a monotonic clock across processes.

Re-emitting the same history index does not add a second logical record. A real
retry is a new index, attempt, or round, and stays distinct.

If the process dies after `job.json` is saved and before the JSONL append, the
next command rebuilds what it can from `history`. Rebuilt events may have a
null `attempt` and `round`, because history does not store those numbers.
Agent timings that were never observed are not invented.

## Configuration fingerprint

`config_hash` is `factory_config_hash` from `config.snapshot`. It is SHA-256 of
canonical JSON of the merged config.

Excluded from the hash:

- `[telemetry]` itself (it does not change execution)
- `email`, `token`, `token_file`, `token_env`, `email_env`, `password`, `authorization`, `api_key`, `secret`
- absolute paths and credential-shaped strings

Included: pipeline steps (`pipeline_hash`, also copied to `graph_hash`), role
file bytes under `.factory/roles/`, a hash of the configured skill list, and
root `AGENTS.md` / `CLAUDE.md` when those files exist. Skill and rule files
outside the repository are not hashed. The hash cannot rebuild uncommitted
file contents. `adapter_version` is null.

`[telemetry.privacy] store_config_content = true` also writes the sanitized
config to `.factory/telemetry/snapshots/`. Default is off.

## Usage and cost

| Provider | What is stored |
| --- | --- |
| Claude Code | Input, output, and cache tokens when a subagent transcript's prompt contains `Job folder:`. `cost_usd` is an **estimate** from the adapter price table (`cost_kind` `estimated`, `price_table_version` set). Not an invoice. `[tools.claude] cost = false` turns this reader off, same as the job's cost panel. |
| Cursor | Tokens only when a transcript message has a `usage` object. No price table ships here, so `cost_usd` is null. |
| Codex | No usage interface. Token and cost fields are null (`cost_kind` `unavailable`). |

Claude totals are cumulative for the job folder. Each `usage.recorded` from
that source has `aggregation` `cumulative_snapshot`. Do not sum those
`cost_usd` values. Sum `delta_cost_usd` / `delta_input_tokens` /
`delta_output_tokens`, or keep the latest snapshot per `attribution_key`.
Plan-limit points (`source` `plan_statusline`) and the 7-day machine share
(`source` `machine_share`) are separate and are not dollars.

A step with no transcript gets one `unavailable` record with null tokens and
null cost, not zeros.

`agent.started` is not emitted. The factory does not observe the moment an
external agent process actually starts. `resolved_model` on step events stays
null; models that appear in a transcript are on the usage record.

## Files

```text
.factory/telemetry/project.json          stable project id
.factory/telemetry/health.json           collection errors and tail repairs
.factory/telemetry/snapshots/            only when store_config_content is true
.factory/jobs/<id>/telemetry/events.jsonl
.factory/jobs/<id>/telemetry/manifest.json
```

Writes take an exclusive lock, append one JSON line, and fsync. A crash can
leave a partial last line. The next write, validate, or prune drops that tail
and leaves earlier lines. Complete but invalid lines are reported, not deleted.
This is best-effort, not exactly-once. Export drops a repeated `event_id` or
`occurrence_key`.

`mfactory install` adds `/.factory/telemetry/` to `.gitignore`. Job events sit
under `.factory/jobs/`, which is already ignored.

## Privacy

The payload allowlist is the control. Redaction is the second check. Default
records do not contain prompts, transcripts, tool arguments, tool outputs,
command output, environment variables, issue text, feedback text, logins, or
absolute paths. `[telemetry.privacy] store_prompts` and the other `store_*`
flags except `store_config_content` cannot turn that capture on in this version.

## Commands

```bash
mfactory telemetry status
mfactory telemetry events 004
mfactory telemetry events 004 --type step.completed
mfactory telemetry validate
mfactory telemetry export --format jsonl
mfactory telemetry export --type step.completed --format csv
mfactory telemetry prune
mfactory telemetry prune --dry-run
```

`validate` exits 1 when it finds a problem. `export` without `--job` writes
every job. CSV is the envelope columns; with `--type`, it adds that type's
payload fields instead of a JSON payload column. `prune` deletes events older
than `retention_days` and does not touch a job that is still unfinished.
Nothing is pruned automatically.

`status` prints collection health, event counts, and the provider limits above.
It does not call a model.

## Failure behavior

`[telemetry] fail_open` defaults to true. Permission errors, a full disk, and
bugs inside the collector are written to `health.json` when that write itself
works, and the factory command still returns the job result. Set `fail_open`
to false only when a telemetry failure should fail the command. The job file
may already have been saved.

`[telemetry] enabled = false` creates no telemetry files.

## Extension

Sinks implement `publish(event)`. The factory calls `Collector.emit(...)`. A
future Hub exporter can implement the same sink and read the same JSONL. It is
not part of this version. Do not add a second event schema.
