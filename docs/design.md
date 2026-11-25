# Design notes

How the factory works, what guards it, what has not been tested, and what
changed from the project it is derived from.

## Three parts

| Part | What it is | What it decides |
| --- | --- | --- |
| The script, `.factory/bin/mfactory` | Python, standard library only | Everything about state: which step a job is in, where an outcome leads, whether a limit is reached, whether the checks pass, when to merge. |
| The orchestrator, the `/mfactory` skill | A prompt your coding tool's main agent follows | Nothing. It asks the script what to do, starts the agents the script names, and reports back. |
| The agents | One small generated file per step, pointing at a prompt in `.factory/roles/` | The content of their one step: a spec, a build, a review. |

The pipeline is data in `factory.toml`, so no prompt describes it. Changing
the pipeline changes no prompt, and the orchestrator works the same for any
graph.

The orchestrator repeats two commands for each job:

```text
$ .factory/bin/mfactory next 004
start-agent mfactory-spec
prompt: Job: 004-dark-mode. Step: spec. Job folder: /repo/.factory/jobs/004-dark-mode/. ...
then: .factory/bin/mfactory done 004-dark-mode

$ .factory/bin/mfactory done 004-dark-mode
004-dark-mode #12: spec DONE -> build
```

`next` runs `checks`, `merge` and `verify` steps itself and prints the result.
For `agent` and `review` steps it prints which agents to start and the one-line
prompt of each. `done` reads the first line of the file the agent wrote,
checks the work (the file is new, the tree is clean, the branch is pushed),
and follows the edge for that outcome.

## Where things live

In this repository:

```text
core/bin/mfactory          the CLI's entry point
core/bin/factory          the old name, kept so an upgrade from 0.1.0 can sync
core/lib/modfactory/      the factory's code
  cli/                    the command line: parser, install and status
  config.py               factory.toml over the defaults; validation; `config show`
  graph.py                steps, edges, outcomes; validation of the graph
  engine.py               next and done: entering steps, following edges, limits
  scripted.py             the merge step and the verify step
  checks.py               [checks].commands and protected paths
  queue.py                queue, create
  sync.py                 agent and skill files for each tool
  install.py              copy, init, upgrade, doctor
  workspace/              several projects: workspace.toml, one queue, the dashboard
    paths.py              workspace.toml name, schema, and the user file
    project.py            one checkout: binary and jobs folder
    registry.py           load, save, add, locate
    concurrency.py        cap on new and resume rows across projects
    queue.py              run each project's own factory queue
    jobs.py               read each project's jobs, graph, and config, tolerating old files
    command.py            `mfactory workspace` subcommands
    service.py            the dashboard server, and start/stop
  service/                services the factory calls
    gh.py                 GitHub, through the `gh` CLI
    human.py              asking a person and reading the reply
  store.py, cost.py, util.py
core/roles/*.md           one prompt per agent step and reviewer
core/skill/SKILL.md       the orchestrator
core/defaults.toml        the default pipeline and settings
core/templates/factory.toml  the factory.toml written on first install
web/dashboard.html        the dashboard shell: one project, or every project in the workspace with a filter
web/dashboard.css         styles, inlined into the page when it is served
web/dashboard.js          the page's script, inlined the same way
adapters/*.toml           per tool: folders, agent file format, default models
install.sh
tests/
```

In a project, `core/` becomes `.factory/`, with `adapters/` inside it. `web/`
stays in the global install, `~/.mfactory/web/`.

Each job has a folder, `.factory/jobs/<id>/`, in the main checkout and ignored
by git:

```text
job.json                  the job's state: step, history, counts, pull request
issue.md                  the issue and its comments, as quoted data
intake.md  spec.md  build.md  decision.md  verify.md
progress.md               notes from a build attempt that stopped early
feedback-1.md             what a person asked for
round-1/checks.md         the gate's verdict, with checks.log
round-1/review-code.md    one file per reviewer
round-1/debug-1.md        the debugger's diagnosis
```

and a git worktree at `.worktrees/<id>` on branch `{branch_prefix}<id>`
(default `factory/<id>`). A revert branch is `{branch_prefix}revert-<id>`.

## Edges and limits

Following an edge is the core of the pipeline logic. In outline:

```python
def follow(job, step, outcome):
    edge = steps[step]["on"][outcome]
    key = f"{step}.{outcome}"
    if edge.get("max") is not None and job["taken"].get(key, 0) >= edge["max"]:
        return escalate_to, f"[steps.{step}].on.{outcome} max = {edge['max']} reached"
    job["taken"][key] = job["taken"].get(key, 0) + 1
    return edge["to"], None
```

- The count for an edge is how many times in a row the step took it. It
  starts again when the step returns another outcome.
- A switched-off step is passed through as if it had returned its `skip`
  outcome; several in a row are passed through at once.
- A job whose limit is reached goes to `[run].escalate_to`. A person's reply
  there clears every count.
- Entering a `build` step after the job's pull request merged starts a
  follow-up. The merged pull request moves to `prs`, and the job continues on
  a new branch `<prefix><job>-fix-N` from the base, with a new pull request.
  `pr`, `pr_url`, `merge_sha` and `branch` always describe the current pull
  request.
- Whether an agent's file is new is decided by comparing the file's size and
  modification time with what was recorded when the job entered the step, not
  by comparing clocks.

## What guards it

- **The label is the trust boundary.** Only issues with the `factory` label
  run, and adding a label needs triage or write access.
- **Issue text is data.** It reaches agents only inside `issue.md`, which every
  prompt calls quoted data. The orchestrator never pastes it into a prompt.
  This lowers the risk of an issue steering an agent; it does not remove it.
- **Intake has no shell.** The step that reads an unreviewed issue first
  cannot run commands. See below.
- **The checks are a script.** No agent's opinion can pass a build whose checks
  fail.
- **Protected paths.** The checks fail any branch that touches `factory.toml`,
  `.factory/`, a generated agent or skill file, or a path in
  `[paths].protected`. A job cannot change the pipeline, the checks or its own
  judges.
- **The factory merges only when the merge step is on.** By default that step
  is off: a maintainer merges, and a hand merge continues from the merge
  step's `MERGED` edge. When the step is on, the script merges only after the
  base branch is merged in and the checks pass again. The factory never
  deploys and never merges a revert.
- **Replies are checked.** A reply to a human step counts only from the
  issue's author, from someone with write access, from the operator, or from
  named logins, depending on the step. `replies = "tool"` ignores comments
  and takes the reply from the coding tool.
- **The dashboard changes nothing.** It answers only on `127.0.0.1`, refuses
  requests with another `Host` header, serves only `.md` and `.log` files from
  `.factory/jobs/`, loads nothing from the network, and builds its page
  without ever turning text into markup, because job text comes from GitHub
  issues. Agent activity sends the tool name and a short label only, not tool
  output, file contents, or assistant replies.

## Differences between the tools

| | Claude Code | Cursor | Codex |
| --- | --- | --- | --- |
| Agent files | `.claude/agents/*.md` | `.cursor/agents/*.md` | `.codex/agents/*.toml` |
| Skill | `.claude/skills/mfactory` | reads Claude's, else `.agents/skills/mfactory` | `.agents/skills/mfactory` |
| Model per step | `model:` | `model:` | `model =`, left out for `inherit` |
| Effort per step | `effort:` | `model: name[effort=x]`, only with a named model | `model_reasoning_effort =` |
| Intake's guard | No shell tool; writes its file | `readonly: true`; hands in its file as its reply | `shell_tool = false`; hands in its file as its reply |
| Project skills | Named in the prompt, and preloaded when in `.claude/skills` | Named in the prompt | Named in the prompt |
| User skills | Named in the prompt with a `~/` path | Named in the prompt with a `~/` path | Named in the prompt with a `~/` path |
| Plugin skills | Invoked with the Skill tool; `Skill` is added to an agent that needs it | Invoked by name, with a `~/` path when the plugin is installed | Mentioned as `$plugin:skill`, with a `~/` path when the plugin is enabled |
| Instruction files | Named in the prompt when `rules` lists them. Empty `rules` still reads `AGENTS.md`, or `CLAUDE.md` when that is missing | Named in the prompt when `rules` lists them | Named in the prompt when `rules` lists them |
| Cost per job | From transcripts | Not recorded | Not recorded |
| Builder stop hook | Optional | No | No |

The prompts themselves are the same for every tool. A generated agent file
holds only the tool's own settings, a pointer to the role file, and the
sections that come from `factory.toml`.

### Intake has no shell

Intake reads an issue nobody has reviewed, so it gets the fewest abilities
each tool allows.

- In Claude Code, intake has the read and write tools and no shell.
- In Cursor, the only restriction an agent file offers is `readonly`, which
  also stops intake writing `intake.md`. So intake replies with the file's
  contents, and the orchestrator passes the reply to
  `factory done <job> --reply`, which checks it and writes the file.
- In Codex, intake's file sets `shell_tool = false` and hands in its reply the
  same way. Whether a Codex agent without a shell can still read the project's
  files could not be tried here. If it cannot, set
  `[tools.codex] intake_shell = true`; intake then has a shell on Codex, which
  is a weaker guard.

## Decisions the plan left open

- **A package, not one file.** The CLI is `core/bin/mfactory` plus the
  `modfactory` package beside it. The command line lives in `modfactory/cli/`.
  It still installs as one folder with no dependencies.
- **A shared role file.** `roles/common.md` holds what every agent must know
  (the prompt's format, the rules, the file format), so each role file holds
  only its own step.
- **Bare edges keep the default limit.** `on = { CHANGES = "rework" }` changes
  the target and keeps `max = 2`, because tables merge key by key. Without
  this, renaming a target would silently remove a loop limit.
- **`default` models.** `[models.<tool>].default` applies to every agent that
  names no value of its own, so one line gives Cursor or Codex a model for
  every step.
- **Skill copies.** A tool gets its own copy of the `/mfactory` skill only when
  no copy already sits in a folder it reads.
- **`doctor` runs the checks** on a clean checkout of the base branch, unless
  told not to, because a base branch that fails its own checks fails every
  job.
- **The dashboard reads each request.** The server rebuilds the project list
  from each checkout's job files. `index.json`, `graph.json`, and `config.json`
  are used when that project has written them. With no `graph.json`, the
  pipeline is read from `factory.toml`, so an empty project still draws.
- **A workspace is a list of checkouts.** The default file is
  `~/.mfactory/workspace.toml`. `install.sh --global` creates `~/.mfactory`.
  `mfactory install` and `mfactory workspace add` write the workspace file.
  A `workspace.toml` in the current directory or a parent, or
  `FACTORY_WORKSPACE`, wins. The workspace runs each project's own
  `mfactory queue` and reads that project's job files. It does not merge
  pipelines or move a job between repositories.
  The dashboard is one page for every project in the workspace, with a filter
  for all of them or one. Each project's pipeline stays its own.
  Each project applies its own `[run].concurrency` first. The workspace then
  applies `[workspace].concurrency` from this factory's `defaults.toml`.
  `concurrency` in `workspace.toml`, or `--concurrency` on `workspace queue`,
  overrides that cap.
- **The dashboard lives in the global install.** `web/` is not copied into a
  project's `.factory/`. A project's binary hands `dashboard`, `start` and
  `stop` to the global `mfactory`, so a machine has one page and one version
  of it.
- **Job files carry a schema number.** `job.json` and `index.json` say which
  shape they have. The global reader fills fields an older factory did not
  write and shows a note to upgrade that project. Files from a newer factory
  are not shown; the note says to upgrade the global CLI.
- **Overview is the job table.** Running slots, a running total of merged jobs
  with a steady-pace line, and cost or time by step. Not jobs per day: with a
  few merges a week, a per-day chart is mostly zeros. The board stays jobs by
  step, with a Now panel of the agents still on the current step.
- **The pipeline view is a column of steps** with forward edges on the right
  and back edges on the left. A left-to-right drawing of twelve steps was too
  small to read.

## What is untested

The tests run whole jobs through the real script in temporary git
repositories, with a stand-in for `gh` and with Python functions playing the
orchestrator and the agents. They cover the graph, limits, skipped steps,
human steps, concurrency, checks and protected paths, the debug and verify
steps, the configuration, skills, the files written for each tool, install,
upgrade, doctor and the dashboard's server.

They do not cover anything that needs a real tool or the real GitHub:

- **No job has run in Claude Code, Cursor or Codex.** None of them was
  available where this was built. The generated agent files follow each
  tool's documentation, but no tool has loaded them.
- Whether each tool's main agent follows the orchestrator skill reliably:
  starts agents by the exact name, in parallel, in the background, and passes
  prompts unchanged.
- Handing in intake's file as a reply, on Cursor and Codex.
- Whether a Codex agent with `shell_tool = false` can read files.
- Whether Cursor lists the `/mfactory` skill twice when Claude Code, Cursor and
  Codex are all enabled. With only Claude Code and Cursor there is one copy.
- Model names. The factory does not check them, and the defaults for Claude
  Code are the aliases the reference project used.
- The real `gh`: the factory calls `gh api`, `gh issue`, `gh pr` and
  `gh label` with the flags documented for them, but only the stand-in has
  answered.
- `install.sh` downloading from GitHub. The tests install from a local copy.
- Windows. The script uses `fcntl` and `bash`, so it needs macOS, Linux or
  WSL.

The first real run should be on a throwaway repository.

## What changed from the reference

modular-factory is derived from
[Software Factory for Claude Code (v2)](https://github.com/zazencodes/zazencodes-season-3/tree/main/src/software-factory-claude-code-v2)
by ZazenCodes, under the GPLv3. The job state handling, the checks gate, the
merge flow, the cost calculation and the agent prompts started from that
project's `factory/bin/job`, `factory/bin/gate`, `factory/bin/route` and
`.claude/agents/*.md`. Changes, all made in 2026:

- **Installable.** The reference is a folder copied into one repository. This
  is a versioned release that an installer puts into any repository, with
  `init`, `upgrade` and `doctor`.
- **Three tools.** The reference runs in Claude Code. Prompts here have no
  tool-specific wording, and `mfactory sync` writes each tool's agent files
  from the same source.
- **One configuration file.** `factory/config.json`, `factory/checks.sh` and
  settings held in agent files became `factory.toml`, merged over defaults and
  validated with errors that name the key.
- **One CLI.** `job`, `gate` and `route` became `factory`, which finds the
  project from the git root instead of from its own location.
- **The pipeline is a graph.** The reference's stages were fixed in the script
  and described in the orchestrator's prompt. Here steps and edges are data;
  steps can be added, switched off or rewired, and the orchestrator's prompt
  describes none of them.
- **Limits per edge.** One `max_rounds` became a `max` on each edge that goes
  back. A failing gate now uses the gate edge's count and no longer consumes a
  review round.
- **One way of waiting.** The `needs-info` and `needs-human` states became the
  `ask-author` and `escalated` human steps. Any step can be a human step, and
  replies are read from the tool or from comments.
- **New optional steps:** approval of the spec by a person, a debugger after
  failing checks, and validation after merge or deploy.
- **Models and effort per step and per tool**, with variants by risk. This
  replaces the routing table, which set models only for reviewers.
- **Reviewers and surfaces are data.** A new reviewer or surface needs a few
  lines of configuration and no prompt file.
- **Skills** are passed to agents explicitly, per step: a project skill, a user skill, a plugin skill, or `all`.
- **Instruction files** (`AGENTS.md`, `CLAUDE.md`, and rule files) are passed the same way through `rules`. Empty `rules` still reads `AGENTS.md`, or `CLAUDE.md` when `AGENTS.md` is missing.
- **Checks are a list of commands**, not a shell script, and stop at the first
  failure.
- **Every round starts a fresh builder** that reads `build.md`, `progress.md`
  when a previous attempt of that step stopped early, and the git log.
  Resuming the previous builder by message is not the default, because not
  every tool can do it. `done` deletes `progress.md` once the build outcome is
  accepted.
- **The stop hook, the cost and resuming the builder** are optional and Claude
  Code only. With `resume_builder`, `next` prints a `name:` that carries the
  job, model and effort. The orchestrator wakes the builder it started under
  that name with SendMessage. `next` leaves the name out when `progress.md`
  shows the last builder stopped early.
- **The dashboard was rewritten** with board, pipeline, jobs, overview and config, a new look, and a local
  server that confines what it serves.
- **Tests.** A suite that runs whole jobs against a stand-in for GitHub.
