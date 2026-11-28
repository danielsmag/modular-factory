# Plan: the planner (duplicates, dependencies, priority, alerts, summary)

## Context

Jobs keep colliding: two tickets describe the same logic in different words, or one
job depends on another that hasn't landed. Today nothing looks across tickets; each
job runs alone (`queue.py:177-235`). The user wants a **planner agent** that sees the
whole picture across GitHub, Jira and factory jobs, flags problems with a suggested
action, and lets the user decide. They also want to talk to it, steer jobs through it,
see a summary on the dashboard, and get alerts.

Decided with the user:
- Two modes: a **scan** on a cheap model, and a **chat** with the planner in the coding tool.
- The planner **flags and suggests**. Nothing changes until the user approves.
- Alerts: a **dashboard badge**, plus an optional **browser notification** sent from the dashboard page.
- The planner **sees priority**. It comes from Jira priority, GitHub labels (with a mapping)
  and, as an option, a GitHub Projects field. All three are supported.
- Runs at **high, medium or low frequency, or on demand**.
- **Workspace-wide from day one**, including duplicates and dependencies across repositories.

My calls (each can be overruled):
- **The script finds candidates and the cheap model judges them.** A cheap model looking at
  everything is noisy and costly. Plain rules miss the "same logic, different words" case.
- **Each finding carries one suggested action**, from a fixed list the script knows how to
  run: link, hold until another job is done, add context, close as duplicate, move card,
  comment. Each action names its targets, and the script checks them against the snapshot.
- **The dashboard stays read-only.** It gets one new GET route. Approval happens in the
  coding tool (`mfactory plan approve F-12`, or by asking the planner in chat).
- **"Hold" is a field on the job, not a pipeline step**, so the graph stays untouched.

## Approaches considered

- **Chosen: the script builds a snapshot and candidate pairs, the cheap agent judges them,
  and the CLI applies what the user approves.** Cost is bounded because the candidates are
  capped, every change goes through the script, and it matches the factory's split
  ("the script decides state, agents decide content").
- **Rejected: the model reads every ticket on every scan.** Cost grows with the square of
  the ticket count, and results are noisier.
- **Rejected: rules only.** They miss tickets that describe the same logic in different words.
- **Rejected: the dashboard applies actions.** That breaks the read-only, localhost-only,
  no-write-route rule (`docs/design.md`, the "What guards it" section).

## Part 1: The snapshot and candidates (script, no model)

`core/lib/modfactory/planner/` (new package):
- **`snapshot.py`, `build_snapshot(factory)`**, per project. It collects:
  - the issues in the tracker's queue (`queue_and_issues()`)
  - unfinished jobs (`store`), with their step, branch, PR and `issue.md`
  - the files each branch touches (`git diff --name-only base...branch` in the worktree)
  - each ticket's title, body excerpt and labels, plus its normalized priority:
    `critical | high | normal | low`
- **`priority.py`**:
  - **Jira.** Add `priority` to `LIST_FIELDS` (`service/tracker/jira.py:25`) and map the
    name to a level, configurable.
  - **GitHub labels.** `[planner.priority.labels]` maps label names to levels, with
    defaults such as `P0/critical`, `P1/priority:high`, `P2`, `P3/low`.
  - **GitHub Projects (optional).** `[planner.priority.github_project] owner, number,
    field = "Priority"`, read with `gh project item-list --format json` and cached.
  - Unknown → `normal`.
- **`candidates.py`** produces scored pairs from stdlib rules, capped at
  `[planner] max_pairs` (default 40):
  - token similarity of title and body (Jaccard plus `difflib`)
  - explicit references (`#12`, `PROJ-7`, `depends on` / `blocked by` / `duplicate of`)
  - files touched by more than one branch
  - shared labels or components
- **Workspace.** `mfactory workspace plan collect` runs each project's
  `project.binary plan snapshot --json`, with the same pattern and old-core tolerance as
  `collect_queue` (`workspace/queue.py:76-131`). It merges the results, computes
  cross-project candidates, and writes `user_dir()/planner/snapshot.json` with a content hash.

## Part 2: The scan agent (cheap model)

- **A new agent key, `planner-scan`,** outside the pipeline:
  - **Config.** Add it to `Config.agent_keys()` (`config.py:255`), or to an
    `extra_agent_keys()` that both validation (`config.py:962`) and `sync.agents()`
    (`sync.py:568`) use. That way `stale()` (`sync.py:872`) keeps the file.
  - **Default models.** In `adapters/claude.toml`: `planner-scan = { model = "haiku", effort = "low" }`.
    Cursor and Codex get effort-only entries. The project overrides it with
    `[models.<tool>].planner-scan`.
  - **Role.** A new `core/roles/planner.md` with its own prompt (`common.md` assumes a
    job). The agent reads `snapshot.json`, treats all ticket text as quoted data, and
    writes `findings-draft.json`.
- **What the agent writes.** One entry per real problem:
  - kind: `duplicate | dependency | overlap | conflict | priority-inversion`
  - the tickets involved
  - confidence and evidence
  - one suggested action from the fixed list
  - It may also add pairs that are not among the candidates, capped at `max_extra`.
- **`mfactory plan ingest`:**
  - checks the draft against a schema and the snapshot. It drops targets that are not in
    the snapshot and actions outside the list.
  - computes **severity**. The script does this, not the model, from the kind, the
    highest priority involved, and the stage. For example, a duplicate where both jobs are
    in build is `high`, and a high-priority job depending on a low-priority one is a
    `priority-inversion`.
  - merges with the existing findings. The key is the kind plus the sorted tickets. A
    dismissed finding stays dismissed until one of its tickets' content hash changes.
  - writes `user_dir()/planner/findings.json`.
- **Frequency.** `[planner] frequency = "high" | "medium" | "low" | "manual"` means at
  most one scan every 5, 30 or 240 minutes.
  - **`mfactory plan due`** prints `scan` only when that interval has passed **and** the
    snapshot hash changed, so an unchanged queue costs nothing.
  - **The `/mfactory run` loop** calls `plan due` between passes. When the answer is
    `scan`, it follows the printed `start-agent mfactory-planner-scan` /
    `then: mfactory plan ingest` lines, the same shape as `next`
    (`core/skill/SKILL.md:51-80`).
  - **On demand.** `/mfactory plan scan`.
  - **Claude Code.** `/loop` also works for users who want it without running the queue.

## Part 3: Acting on findings (script, user approves)

- **CLI group `mfactory plan snapshot|scan|due|ingest|summary|show|approve|dismiss|release`.**
  - It follows the subparser pattern of `tracker`/`close` in `cli/parser.py:112-152`, and
    dispatch in `cli/__init__.py:433+`.
  - `approve F-n` prints the exact action, runs it in the owning project through
    `project.binary plan apply ...`, and records who approved and when.
- **Actions** (`planner/apply.py`):
  - **link.** A new tracker method `link(key, other, kind)` in `service/tracker/base.py`,
    `routing.py`, `github.py`, `jira.py` and `jira_acli.py`. On GitHub it posts a comment
    ("Blocked by #A" / "Duplicate of #A"). On Jira it uses `POST /rest/api/3/issueLink`,
    with an acli equivalent.
  - **close as duplicate.** Link it, then `close_issue(key, "not planned")`. If a job
    exists, also `close job`.
  - **hold** (B waits for A):
    - B gets `job["held_by"] = {project, issue, reason, since}`. If B has no job yet, the
      hold goes into `.factory/jobs/holds.json`.
    - `queue.py` (~:203) emits `wait … held until <A> is done` and keeps B out of the
      slots. `_picks` skips held fresh issues.
    - `store.running()` / `holds_slot` (`store.py:108-115`) exclude held jobs. Otherwise a
      held job would still take a slot.
    - `engine.next` (`engine.py:455`) refuses a held job before `_has_turn`.
    - `runstate.job_status` returns `held`.
    - **Auto-release.** A hold lifts when A reaches an end state (`graph.END_STATES`).
      For a hold within one project, `queue` releases it. For a hold across projects,
      `workspace plan collect` releases it, because it sees every project.
  - **add context.** Write `context-N.md` in the job folder (the same writer shape as
    `_apply_reply`, `service/human.py:197`) and add it to a new persistent
    `job["context_files"]`. `engine._prompt` (`engine.py:395`) always appends it, unlike
    `handoff`, which is cleared after a step. `build.md`, `spec.md` and `approve.md`
    mention context files.
  - **move card** and **comment** use the existing `move` and `tracker.comment`.

## Part 3b: On-demand summary, `mfactory plan summary`

The user asks for the whole picture whenever they want it, in Claude Code or any
supported tool.

- **CLI: `mfactory plan summary [--fresh] [--project P] [--json]`** (`planner/summary.py`).
  It builds the snapshot first (`workspace plan collect`), so it is never stale, then prints
  a readable report from the snapshot and findings. No model is needed and it is instant.
  The report has these parts, in order:
  1. **Needs attention.** Open findings at or above `alert_at`, highest first: kind, the
     tickets with project and priority, and the suggested action with its `approve` command.
  2. **Other findings.** Grouped as duplicates, dependencies, overlaps and priority
     inversions.
  3. **Held jobs.** What each one waits for, and since when.
  4. **Pipeline at a glance.** For each project: how many jobs are queued, running and
     waiting on a person, by step. The high and critical tickets are listed with their stage.
  5. **Last scan.** When it ran, whether the snapshot has changed since, and when the next
     scan is due.
- **`--fresh`** runs a scan now, ignoring `frequency`. The CLI can't start an agent itself,
  so it prints the `start-agent mfactory-planner-scan` and `then: mfactory plan ingest`
  lines, followed by `then: mfactory plan summary`.
- **In the tool: `/mfactory plan summary`.**
  - The orchestrator runs `mfactory plan summary --fresh`, which scans only when the
    snapshot changed since the last scan.
  - Once that finishes, it presents the report in plain words, still on the session's
    model, and offers to act on any item. That is where chat mode (Part 4) starts.
  - SKILL.md also routes "give me a summary", "what's the status of everything" and
    "what overlaps" here.

## Part 4: Chat mode

- A `/mfactory plan` section in `core/skill/SKILL.md`, plus a row in the command table and
  the trigger words in the frontmatter description.
  - The main agent (the session's own, stronger model) runs `mfactory plan show --json`
    and reads the snapshot, then discusses with the user.
  - It may draft new suggestions with `mfactory plan suggest` (checked by ingest), and runs
    `approve`, `dismiss`, `release` or the existing `move`/`close` only when the user says so.
- The `<anything else>` section (`SKILL.md:264`) points questions like "what overlaps?"
  or "what should run next?" to the planner.

## Part 5: Dashboard

- **New GET route `/planner.json`** in `workspace/service.py` `_routes()` (:283). It
  returns the findings, `generated_at`, `next_scan_due` and `errors`. Every string goes
  through `activity.redact`, and only links that pass `WEB_URL` are kept.
- **Header badge** next to `#live`: the count of open findings, coloured by the highest
  severity (reusing `.dot.good/warn/stop`). Clicking it opens `#/planner`.
- **A new view `#/planner`**, a nav entry plus a branch in `render()` (`dashboard.js:2954`).
  Each finding shows:
  - kind and severity
  - the tickets, with links, project and priority chips (`priorityClass`, js:1719)
  - stage and evidence
  - the suggested action, with the exact command to copy
  - The view is built only with `el()`/`link()`, with no markup from text.
- **Board.** A small flag on the job and issue cards involved in an open finding.
- **Browser notifications:**
  - An opt-in toggle in the planner view calls `Notification.requestPermission()`.
  - The page sends one notification per new finding at or above `[planner] alert_at`
    (default `high`).
  - Seen finding ids go into `localStorage` (wrapped in try/catch) so nothing repeats.
  - `refreshPlanner` polls every 15 s, like `refreshQueue`.
  - The CSP is unchanged; plain `new Notification()` needs no worker.

## Config (`core/defaults.toml`, `core/templates/factory.toml`, `config.py` `check()`, `docs/configuration.md`)

```toml
[planner]
enabled = true
frequency = "medium"        # high | medium | low | manual
alert_at = "high"           # severity that notifies
max_pairs = 40
[planner.priority.labels]   # label -> level
"P0" = "critical"
"priority:high" = "high"
[planner.priority.github_project]   # optional
owner = "acme"
number = 3
field = "Priority"
```

## Tests (pytest, offline stubs; `python3 -m pytest -o addopts=-q`)

- **`test_planner_snapshot.py`:**
  - snapshot contents across GitHub, Jira and both
  - priority from Jira, from labels, and from a Projects field (extend `stub_gh.py` with
    `project item-list`)
  - candidates found from similar titles, explicit references and shared files
- **`test_planner_ingest.py`:**
  - schema rejections, unknown targets dropped, severity rules
  - dismissal memory, and the dismissal resetting when a ticket's hash changes
  - `due` honouring both the interval and the hash
- **`test_planner_apply.py`:**
  - link on GitHub (comment) and Jira (issueLink in `stub_jira.py`, plus acli)
  - close duplicate
  - hold, including hold-skips-slot, `next` refusing, auto-release and a fresh-issue hold
  - context file persisting across two steps
- **`test_planner_summary.py`:**
  - the section order, with "needs attention" sorted by severity and then priority
  - `--json` shape and `--project` filter
  - `--fresh` printing the scan lines only when the snapshot changed
  - an empty workspace printing "nothing to flag"
- **`test_workspace_planner.py`:** two projects, cross-project candidates, cross-project
  hold released by `collect`.
- **`test_dashboard_feeds.py`:** `/planner.json` shape, redaction, Host check.
  `test_dashboard_browser.py` covers the badge and view and the notification toggle (with
  `Notification` stubbed in Playwright).
- **Sync:** the `mfactory-planner-scan` agent file is generated for each tool,
  `mfactory models` lists it, and `stale()` keeps it.

## Delivery

Before any code (brainstorming flow):
1. Write this up as `docs/superpowers/specs/2026-11-28-planner-design.md` and commit it.
2. Run `superpowers:writing-plans` to produce `docs/superpowers/plans/2026-11-28-planner.md`.

Then build on branch `claude/festive-pascal-y98xtd`, in this commit order:
1. Read priority from Jira, GitHub labels and GitHub Projects.
2. Build the planner snapshot and candidate pairs, per project and across the workspace.
3. Add the planner-scan agent, ingest, severity and the scan schedule.
4. Hold a job until another is done.
5. Add context files that every later step reads.
6. Link, close as duplicate, and the `plan approve|dismiss|release` commands.
7. Add `mfactory plan summary`, the whole picture on demand.
8. Talk to the planner from `/mfactory plan`.
9. Show planner findings on the dashboard, with a badge and browser notifications.
10. Document the planner.

Suggested PR title: `feat: a planner that finds duplicate and dependent tickets across the workspace`.

## Verification

- `make check` (ruff, `bash -n`, pytest). Run pyright on the new modules.
- `make sample-dashboard`, with sample findings added to `tests/sample_dashboard.py`.
  Check with Playwright that the badge count and colour, the `#/planner` view and the
  notification toggle all work in light and dark mode.
- End to end on the stubs:
  1. `world.cli("plan", "snapshot", "--json")`, then a scripted `findings-draft.json`
  2. `plan ingest`, then `plan show`
  3. `plan approve F-1` (hold), then `queue` shows `wait … held until`
  4. finish the blocking job, then the hold lifts.
