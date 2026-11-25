# Configuration reference

`factory.toml`, at the root of the repository, is the only file you edit. It
holds what your project changes; everything else comes from the factory's
defaults in `.factory/defaults.toml` and from each tool's adapter in
`.factory/adapters/`.

The files merge key by key. To change one value you write only that value:

```toml
[steps.review]
on = { CHANGES = { max = 3 } }     # the other edge of review, and its target, stay as they are
```

After a change:

```bash
.factory/bin/mfactory sync          # rewrite the generated agent files
.factory/bin/mfactory doctor        # check the result
```

To see what is in effect:

```bash
.factory/bin/mfactory config show   # every setting and which file set it
.factory/bin/mfactory graph         # the pipeline
.factory/bin/mfactory models        # the model and effort of every agent, per tool
.factory/bin/mfactory skills        # project, user and plugin skills, and who reads them
```

A mistake is reported with the key to fix, and nothing runs until it is fixed:

```text
mfactory: factory.toml has 2 problem(s):
  - [steps.review].on.CHANGES points at unknown step "biuld". Steps: intake, ask-author, spec, ...; end states: done, failed, rejected
  - [models.claude].build.effort = "extreme" is not an effort level claude documents. Known: low, medium, high, xhigh, max
```

## Contents

- [Project, tools and run](#project-tools-and-run)
- [Checks](#checks)
- [The pipeline: steps](#the-pipeline-steps)
- [Loop limits](#loop-limits)
- [Who merges](#who-merges)
- [Optional steps](#optional-steps)
- [Adding a step](#adding-a-step)
- [Human steps](#human-steps)
- [Validation after merge or deploy](#validation-after-merge-or-deploy)
- [Reviewers and surfaces](#reviewers-and-surfaces)
- [Models and effort](#models-and-effort)
- [Project skills](#project-skills)
- [Labels, paths and ports](#labels-paths-and-ports)

## Project, tools and run

```toml
schema = 1

[project]
description = "Invoicing app for small clinics. Not a general accounting tool."
base_branch = "main"
merge_method = "squash"

[tools]
enabled = ["claude", "cursor"]

[run]
concurrency = 3
```

| Key | Default | Meaning |
| --- | --- | --- |
| `schema` | `1` | The version of this file's format. A release that needs another schema is skipped until this number matches. |
| `[project] description` | `""` | What the project is for and what does not belong in it. Intake judges issues against it. |
| `[project] base_branch` | `"master"` | Jobs branch from `origin/<base_branch>` and merge into it. |
| `[project] merge_method` | `"squash"` | `merge`, `squash` or `rebase`. With `squash`, the pull request title becomes the commit on the base branch. |
| `[project] pr_title` | `""` | Example pull request title, such as `feat: add CSV export [ABC-123]`. The builder uses this shape instead of the spec title. Empty means the spec title. The script does not fill placeholders. |
| `[project] pr_title_pattern` | `""` | Regular expression the whole title must match (`fullmatch`). Empty means no check. `done` refuses a `READY` build whose title does not match. |
| `[project] branch_prefix` | `"factory/"` | Concatenated with the job id. Include the slash: `feature/` names the branch `feature/001-add-dark-mode`. A revert branch is the prefix plus `revert-` and the job id. |
| `[project] branch_pattern` | `""` | Regular expression the whole branch name must match (`fullmatch`). Empty means no check. The factory checks the name when it chooses the branch, before creating it. A job already on disk keeps the branch stored in its `job.json`. |
| `[tools] enabled` | `["claude"]` | The tools to write agent files for: `claude`, `cursor`, `codex`. |
| `[tools.claude] stop_hook` | `false` | Run the checks when the build agent stops, and send it back to work if they fail. Claude Code only. |
| `[tools.claude] resume_builder` | `false` | In later rounds, wake the job's builder with SendMessage instead of starting a new one, so it keeps what it read. It is resumed only by the session that started it and only while its model and effort are unchanged. Claude Code only. |
| `[tools.claude] cost` | `true` | Work out each job's cost from Claude Code's transcripts. Other tools record no cost. |
| `[tools.codex] intake_shell` | `false` | Let the Codex intake agent run commands. See [Design notes](design.md#intake-has-no-shell). |
| `[run] concurrency` | `3` | Jobs this project works on at once. A job waiting for a person, or being validated after merge, does not count. |
| `[workspace] concurrency` | `3` | `new` and `resume` rows kept across every project when this factory runs `workspace queue`. `workspace.toml` or `--concurrency` overrides it. A project's `factory.toml` cannot. |
| `[run] start` | `"intake"` | The step a new job starts in. |
| `[run] escalate_to` | `"escalated"` | The human step a job goes to when a loop limit is reached. |
| `[run] pick` | `"all"` | Which labeled issues `queue` offers as new jobs. `"mine"` means the issues the operator (the `gh` user) opened or is assigned to. Issues it filters out are not listed. Jobs that already exist keep running, and `/mfactory run <n>` still runs any labeled issue. |
| `[run] authors` | `[]` | Also offer issues these logins opened. `"@me"` is the operator. Once `authors`, `assignees` or `pick = "mine"` names anyone, an issue is offered only if its author or one of its assignees is named. |
| `[run] assignees` | `[]` | Also offer issues assigned to these logins. `"@me"` is the operator. |

`pr_title` is what the agents are shown. `pr_title_pattern` is what the script
checks. Either may be set alone. The check runs for every merge method. When
the issue lacks a value the title needs, such as a ticket key, intake returns
`NEEDS-INFO` and asks. It does not invent one. After changing either key, run
`mfactory sync` so the build and intake agents see it.

```toml
[project]
pr_title = "feat: add CSV export [ABC-123]"
pr_title_pattern = '^(feat|fix|perf|refactor|chore|docs|test|ci|build|revert)(\(.+\))?!?: .+ \[[A-Z]+-\d+\]$'
```

`branch_prefix` is concatenated with the job id, so the slash belongs in the
prefix. `branch_pattern` is checked when the factory chooses the name, before
it creates the branch. A job already on disk keeps the branch stored in its
`job.json`. A revert branch is the same prefix plus `revert-` and the job id,
and the pattern has to match that name too.

```toml
[project]
branch_prefix = "feature/"
branch_pattern = '^(feature|fix|chore|docs|test|hotfix)/[A-Za-z0-9._-]+$'
```

## Checks

```toml
[checks]
commands = ["npm ci --prefer-offline", "npm run lint", "npm test -- --run"]
timeout_minutes = 30
```

The commands run in order in the job's worktree with `bash -c`. The first one
that exits non-zero stops the run and fails the step. They run at the `gate`
step, and again in the `merge` step after the base branch is merged in, when
that step is on. The working directory stays that worktree. Each command gets
`FACTORY_CHECKOUT`, the main checkout where the factory is installed, and
`FACTORY_JOB`, the job id. `mfactory checks` runs in the current directory,
which may be a subdirectory or a job worktree. `FACTORY_CHECKOUT` is still
the main checkout. That command has no job, so `FACTORY_JOB` is unset.

A command must not rewrite files: use `prettier --check`, not
`prettier --write`.

The checks also fail any branch that touches a protected path, whatever the
commands say. See [Labels, paths and ports](#labels-paths-and-ports).

Run them yourself in any checkout with `.factory/bin/mfactory checks`.

## The pipeline: steps

Each step is a `[steps.<name>]` table with a `type` and an `on` table that says
where each outcome leads:

```toml
[steps.gate]
type = "checks"
on = { PASS = "review", FAIL = { to = "debug", max = 2 } }
```

An edge is a step name, or `{ to = "<step>", max = <n> }`. The end states are
`done`, `rejected` and `failed`.

| Type | What happens | Outcomes |
| --- | --- | --- |
| `agent` | One agent does the step and writes one file. Its first line is the outcome. | Fixed for the built-in steps (below); yours for a step you add. |
| `checks` | The script runs `[checks].commands`. | `PASS`, `FAIL` |
| `review` | Every reviewer the job needs runs in parallel. | `PASS` if all pass, else `CHANGES` |
| `human` | The script asks a person and parks the job. | Whatever you name in `on` |
| `merge` | When the step is on, the script merges the base branch in, runs the checks, and merges the pull request. It ships off. | `MERGED`, `FAILED` |
| `verify` | The script, and optionally an agent, check the merged change. | `PASS`, `FAIL`, `BLOCKED` |

The built-in agent steps:

| Step | Writes | Outcomes |
| --- | --- | --- |
| `intake` | `intake.md`: is it worth building, at what scope, how risky, which surfaces | `ACCEPT`, `NEEDS-INFO`, `REJECT` |
| `spec` | `spec.md`: what to build and how to tell it works | `DONE` |
| `build` | Code on the job's branch, a draft pull request, `build.md` | `READY`, `BLOCKED` |
| `debug` | `round-N/debug-K.md`: why the checks fail | `FOUND`, `UNKNOWN` |
| `approve` | `decision.md`: ready to merge, or ask a maintainer | `APPROVE`, `ESCALATE` |

Keys every step takes:

| Key | Meaning |
| --- | --- |
| `type` | One of the types above. |
| `on` | The edges. Every outcome the step can return needs one. |
| `enabled` | `false` switches the step off. Jobs pass through it as if it had returned `skip`. |
| `skip` | The outcome a switched-off step counts as. |

`mfactory doctor` and every other command refuse a pipeline in which an edge
points at a step that does not exist, an outcome has no edge, a step cannot be
reached from the start, or a loop has no `max` on any of its edges and no
human step in it.

## Loop limits

A `max` on an edge is how many times in a row a step may send a job that way.
The count starts again when the step returns a different outcome.

```toml
[steps.intake]
on = { NEEDS-INFO = { max = 2 } }      # times intake may ask the author

[steps.gate]
on = { FAIL = { max = 2 } }            # builder attempts to fix failing checks

[steps.review]
on = { CHANGES = { max = 2 } }         # review rounds that may ask for changes

[steps.spec-approval]
on = { CHANGES = { max = 3 } }         # spec revisions a person may ask for
```

Those are the defaults. When a limit is used up, the job goes to the
`escalated` step, a maintainer is asked, and the reason names the edge:

```text
[steps.gate].on.FAIL max = 2 reached
```

The script counts and enforces the limits; no prompt does. A maintainer's
feedback on an escalated job starts every count again.

Writing an edge as a bare step name changes only its target and keeps the
default `max`:

```toml
[steps.review]
on = { CHANGES = "rework" }                       # still max = 2
# on = { CHANGES = { to = "rework", max = 5 } }   # to change both
```

## Who merges

By default the factory does not merge. `approve` leads to `ready-to-merge`,
which marks the pull request ready, labels the issue `needs_human`, and waits
without holding a slot. The merge step stays off. Its `MERGED` edge still says
what happens after a person merges, including when that step is off:

```toml
[steps.ready-to-merge]
type = "human"
who = "maintainer"
ask = "approved: merge it yourself"
shows = "decision.md"
pr_ready = true
on = { FEEDBACK = { to = "build", max = 3 }, REJECT = "rejected" }

[steps.merge]
enabled = false
skip = "MERGED"
```

That wait does not sync the base branch or run the checks again. Branch
protection is the gate before the person merges. The comment tells them that
merging continues the job.

To let the factory merge, turn the step on, point `approve` at it, and switch
`ready-to-merge` off. Enabling the merge step alone leaves it unreachable, and
leaving `ready-to-merge` on after retargeting `approve` does the same:

```toml
[steps.approve]
on = { APPROVE = "merge" }

[steps.ready-to-merge]
enabled = false
skip = "REJECT"

[steps.merge]
enabled = true
```

Do that only when a merge to the base branch does not publish a release. The
script then syncs the base branch, runs the checks again, and merges.

## Optional steps

`spec-approval`, `debug`, `ready-to-verify` and `verify` ship with
`enabled = false`. The merge step does too; turning that one on is the block
above, not a single key.

```toml
[steps.spec-approval]     # a maintainer approves spec.md before anything is built
enabled = true

[steps.debug]             # after failing checks, a debugger finds the cause before the builder retries
enabled = true

[steps.ready-to-verify]   # wait until a person says the deploy is live
enabled = true
who = "operator"

[steps.verify]            # check the change after it is merged or deployed
enabled = true
commands = ["npm run test:smoke"]
```

The debugger changes no code. It writes what fails and why to
`round-N/debug-K.md`, and the next builder reads it. With the debugger off,
failing checks send the job straight back to the builder.

## Adding a step

Add a block and point one edge at it.

```toml
[steps.docs]
type = "agent"
instructions = "Update docs/ for the change on this branch."
on = { DONE = "gate" }

[steps.build]
on = { READY = "docs" }
```

`mfactory sync` writes a `mfactory-docs` agent. It works in the job's worktree,
commits what it changes to the job's branch, and writes `docs.md`, whose first
line is `STATUS: DONE`. The outcomes of your step are the names you give in
`on`.

A long task can live in a file instead of, or as well as, `instructions`.
`instructions_file` is a path from the repo root. The agent reads it after
`step.md`. A missing file, or a path that leaves the repo, fails `mfactory
sync`. Only a custom step can set it. This does not change `schema`.

```toml
[steps.docs]
type = "agent"
instructions_file = "docs/docs-step.md"
on = { DONE = "gate", SKIP = "gate" }
```

```toml
[steps.license-check]
type = "agent"
instructions = "List every new dependency and its licence. FLAG if any is not MIT, BSD or Apache-2.0."
on = { CLEAR = "review", FLAG = "escalated" }
```

Keys of an `agent` step:

| Key | Meaning |
| --- | --- |
| `instructions` | What the agent must do. A step you add needs this or `instructions_file`. |
| `instructions_file` | A file in the repo, read after `step.md`. Path from the repo root. The file must exist. Custom steps only. |
| `writes` | The file it writes in the job folder. Default: `<step>.md`. |
| `context` | Project facts added to the agent's prompt, such as where things live. Works on built-in steps too. |
| `skills` | Skills this agent reads: a project skill, `user:name`, `plugin:skill`, or `all`. |
| `rules` | Instruction files this agent reads: `AGENTS.md`, `CLAUDE.md`, a rule path, `user:CLAUDE.md`, or `all`. |
| `port` | `true` gives the agent a port number for a dev server. |
| `role` | Use a built-in step's prompt under another name: `intake`, `spec`, `build`, `debug` or `approve`. |

Give the step a model if the default does not suit it:

```toml
[models.claude]
docs = { model = "haiku", effort = "low" }
```

## Human steps

A human step posts a comment where the person will see it (the pull request if
there is one, otherwise the issue), labels the issue, and parks the job. It
holds no slot while it waits.

Adding a person's approval, then letting the factory merge. The merge step
has to be on, or the job skips it:

```toml
[steps.release-approval]
type = "human"
who = "maintainer"
ask = "approve the release"
shows = "decision.md"
on = { APPROVE = "merge", CHANGES = { to = "build", max = 2 }, REJECT = "rejected" }

[steps.approve]
on = { APPROVE = "release-approval" }

[steps.ready-to-merge]
enabled = false
skip = "REJECT"

[steps.merge]
enabled = true
```

| Key | Default | Meaning |
| --- | --- | --- |
| `who` | `"maintainer"` | Who may reply on GitHub. `author`: the issue's author (the operator may also reply). `maintainer`: anyone with write access. `operator`: only the signed-in user running the factory. A list of logins, such as `["ada", "grace"]`: only those accounts. The operator is not added to the list. |
| `replies` | `"any"` | `any`: a GitHub comment from someone `who` allows, or `mfactory answer`. `tool`: ignore GitHub comments. Only `mfactory answer` counts. |
| `ask` | | The heading of the comment. |
| `shows` | | A file from the job folder to include in the comment, such as `spec.md`. |
| `pr_ready` | `false` | Mark the draft pull request ready for review when asking. The comment then says that merging the pull request continues the job. |

The person replies in their coding tool:

```text
/mfactory approve 12
/mfactory changes 12 Use the existing Money type instead of a new one.
/mfactory reject 12 Out of scope for this quarter.
```

or with a comment on the issue or pull request, without the number:

```text
/mfactory changes Use the existing Money type instead of a new one.
```

`mfactory answer` is the person running the factory. `who` does not filter it.
With `replies = "tool"`, the question says a comment does not count, and the
commands in it include the issue number.

The word after `/mfactory` is the outcome's name in lower case, so a step with
`on = { SHIP = "merge", HOLD = "build" }` takes `/mfactory ship` and
`/mfactory hold <what to change>`.

- Words given with a reply are saved as `feedback-<k>.md` in the job folder,
  and the next agent is told to act on them.
- `REJECT` ends the job as `rejected`. It needs a reason.
- On a step with an `ANSWER` outcome, such as `ask-author`, any comment from
  the author counts as the answer.
- Comments from anyone else are ignored. With `replies = "tool"`, every comment is ignored.
- Merging the job's pull request by hand continues the job from the merge
  step's `MERGED` edge, including when that step is off. On the default path
  this does not sync the base branch or run the checks again.

## Validation after merge or deploy

`ready-to-verify` sits between merge and verify. It ships off, so a merge still
lands on verify. Turn it on when a person has to say the deploy is live and
nothing the factory runs can tell. `who` and `replies` are the same keys as any
human step. `/mfactory verify` then runs verify. `/mfactory reject` ends the job.

```toml
[steps.ready-to-verify]
enabled = true
who = "operator"

[steps.verify]
enabled = true
after = "merge"
agent = true
url = "https://app.example.com"
commands = [
  "test \"$(curl -fsS \"$FACTORY_DEPLOY_URL/health\")\" = ok",
]
```

A script that can see the deploy leaves `ready-to-verify` off and uses `wait`:

```toml
[steps.verify]
enabled = true
after = "deploy"
wait = "scripts/wait-for-deploy.sh"
commands = ["npm run test:smoke"]
agent = true
url = "https://staging.example.com"
on_failure = "revert-pr"
```

| Key | Default | Meaning |
| --- | --- | --- |
| `after` | `"merge"` | `merge`: validate the merge commit straight away. `deploy`: first run `wait` until it exits 0. |
| `wait` | `""` | For `after = "deploy"`: a command that exits 0 once the merge commit is live. It is run again every 15 seconds until `timeout_minutes`. |
| `timeout_minutes` | `20` | How long to wait for the deploy before the step fails. |
| `commands` | `[]` | Commands run in a clean checkout of the merge commit. All must exit 0. `[checks] timeout_minutes` limits them. |
| `agent` | `false` | After the commands pass, a verifier agent checks the spec's acceptance criteria and its `## Verify after deploy` section against the running result and writes `verify.md`. |
| `url` | `""` | Where the deployed result is, passed to the commands and the agent. |
| `retries` | `1` | How many times to run failing commands again before the step fails. |
| `on_failure` | `"comment"` | What to do when validation fails, below. |

The commands and `wait` get `FACTORY_CHECKOUT`, `FACTORY_JOB`,
`FACTORY_MERGE_SHA`, `FACTORY_PR` and `FACTORY_DEPLOY_URL`. The working
directory is a clean checkout of the merge commit. `FACTORY_CHECKOUT` is the
main checkout, so a command can read a job file that the worktree does not
contain:

```toml
[checks]
commands = [
  "npm test",
  "test -s \"$FACTORY_CHECKOUT/.factory/jobs/$FACTORY_JOB/spec.md\"",
]
```

The same variable is set for `wait`. A token that is not on the merge commit
stays in the main checkout:

```bash
test "$(curl -fsS -H "Authorization: Bearer $(cat "$FACTORY_CHECKOUT/.factory/smoke-token")" \
  "$FACTORY_DEPLOY_URL/version")" = "$FACTORY_MERGE_SHA"
```

A minimal `wait` script:

```bash
#!/usr/bin/env bash
# Exits 0 once the deployed version is the merge commit.
test "$(curl -fsS "$FACTORY_DEPLOY_URL/version")" = "$FACTORY_MERGE_SHA"
```

When verify is on, the spec must include `## Verify after deploy`: what to run
or look at once the change is live. The verifier reads that section. The script
does not turn those lines into commands.

`VERIFY: PASS` ends the job at `done`. `VERIFY: FAIL` ends it as `failed` after
`retries`. `VERIFY: BLOCKED` means the environment could not be reached. The
job goes back to `ready-to-verify` and waits for `/mfactory verify` again. The
fourth `BLOCKED` in a row goes to `escalated`. `BLOCKED` does not comment as a
failure, open an issue, or open a revert pull request. If `ready-to-verify` is
off, there is nobody to ask, so `BLOCKED` fails the job through `on_failure`
instead of running verify again.

A project that replaces `[steps.verify].on` must keep an edge for `BLOCKED`.
The default is `{ to = "ready-to-verify", max = 3 }`.

When validation fails the job ends as `failed`, and:

| `on_failure` | What the factory does |
| --- | --- |
| `comment` | Comments on the pull request with what failed. |
| `issue` | Also opens a new `factory` issue that links the job and the output, so the factory fixes it in a new job. |
| `revert-pr` | Also opens a draft pull request that reverts the merge, for a person to merge. The factory never merges a revert. |

The factory never deploys. A job being validated holds no slot, so a slow
deploy does not block the queue.

## Reviewers and surfaces

The `review` step runs every reviewer the job needs, in parallel. Four ship:
`code` runs on every job; `security` runs on jobs intake marks with the
`security` surface. `ux` and `ui` run on jobs with the `ui` surface, but they
and the surface ship switched off. A project with a user interface switches
them on:

```toml
[reviewers.ux]
enabled = true

[reviewers.ui]
enabled = true

[surfaces.ui]
enabled = true
```

A new reviewer is a table with a `focus`, a `prompt` file, or both. `focus`
alone needs no file:

```toml
[reviewers.migrations]
focus = "Database migrations: reversibility, locks on large tables, data loss."

[surfaces.database]
when = "it adds or changes a migration or a table"
reviewers = ["migrations"]
```

A long checklist can live in a markdown file in the repo. `prompt` is a path
from the repo root. The agent reads that file after `reviewer.md`, and after
`reviewer-<name>.md` when the reviewer is built in. A missing file, or a path
that leaves the repo, fails `mfactory sync`. This does not change `schema`.

```toml
[reviewers.data]
prompt = "docs/review-data.md"

[surfaces.pipelines]
when = "it changes a pipeline, a model, or a warehouse query"
reviewers = ["data"]
```

A surface is something a change can touch. Intake reads each `when` and names
the surfaces a job touches; the job then gets those surfaces' reviewers. A
reviewer that no surface lists runs on every job.

To add a reviewer to a surface that exists, restate the list. The `ui`
surface must be switched on, as above:

```toml
[reviewers.a11y]
focus = "Keyboard access, focus order, labels and contrast of anything a user sees."
port = true

[surfaces.ui]
reviewers = ["ux", "ui", "a11y"]
```

Any reviewer or surface can be switched off with `enabled = false`. A
switched-off reviewer gets no agent file and no row in `mfactory models`. A
switched-off surface is not offered to intake. A switched-on surface that lists
a switched-off reviewer fails validation, so switch the surface off too, or
restate its list.

| Key | Meaning |
| --- | --- |
| `[reviewers.<name>] enabled` | `false` switches the reviewer off. `ux` and `ui` default to `false`; the others to `true`. |
| `[reviewers.<name>] focus` | What this reviewer looks for. A reviewer you add needs this or `prompt`. |
| `[reviewers.<name>] prompt` | A file in the repo, read after the reviewer role. Path from the repo root. The file must exist. |
| `[reviewers.<name>] context` | Project facts added to its prompt. |
| `[reviewers.<name>] skills` | Skills it reads: a project skill, `user:name`, `plugin:skill`, or `all`. |
| `[reviewers.<name>] rules` | Instruction files it reads: `AGENTS.md`, `CLAUDE.md`, a rule path, `user:CLAUDE.md`, or `all`. |
| `[reviewers.<name>] port` | `true` gives it a port for a dev server, for reviewers that look at the running app. |
| `[surfaces.<name>] enabled` | `false` hides the surface from intake. Its reviewers do not run on every job. `ui` defaults to `false`. |
| `[surfaces.<name>] when` | One line telling intake when a change touches this surface. |
| `[surfaces.<name>] reviewers` | The reviewers that run on jobs with this surface. |

Each reviewer writes `round-N/review-<name>.md` with `VERDICT: PASS` or
`VERDICT: CHANGES` on its first line.

## Models and effort

Each tool has its own table, because model names differ between tools:

```toml
[models.claude]
build = { model = "opus", effort = "high" }
spec = { effort = "high" }                        # keeps the default model
reviewer-code = { model = "sonnet", effort = "medium", low = { model = "haiku", effort = "low" } }

[models.cursor]
default = { model = "gpt-5.6-sol", effort = "medium" }
build = { model = "claude-opus-5-5", effort = "high" }

[models.codex]
default = { model = "gpt-5.6-sol" }
```

- The key is the step's name, or `reviewer-<name>` for a reviewer.
- `default` applies to every agent of that tool that names no value of its
  own. A step's entry sits on top of it, key by key.
- `low`, `medium` and `high` inside an entry apply to jobs of that risk, which
  intake decides. The factory writes one extra agent file per variant, such as
  `mfactory-reviewer-code-low`, and starts the right one.
- `model = "inherit"` uses the model of the session that runs `/mfactory run`.

Defaults:

| Tool | Models | Effort |
| --- | --- | --- |
| Claude Code | `opus` for spec, build, debug, approve and the security reviewer; `sonnet` for the code and UX reviewers and verify; `haiku` for the UI reviewer and low-risk code review; `fable` for intake | Set per step |
| Cursor | `inherit` for every step | Not set |
| Codex | `inherit` for every step | Set per step |

Cursor and Codex default to `inherit` because the model names you can use
depend on your plan. Cursor takes an effort only together with a named model;
`mfactory sync` says so when an effort would be ignored.

`.factory/bin/mfactory models` prints the result:

```text
agent                      claude           cursor                  codex
mfactory-intake             fable / high     gpt-5.6-sol / medium    gpt-5.6-sol / high
mfactory-spec               opus / high      gpt-5.6-sol / medium    gpt-5.6-sol / medium
mfactory-build              opus / high      claude-opus-5-5 / high  gpt-5.6-sol / high
...
```

Effort levels the factory accepts: `low`, `medium`, `high`, `xhigh`, `max` for
Claude Code; those and `none`, `minimal` for Cursor and Codex. The factory
does not check model names: a name your tool does not know fails when the
agent starts.

## Project skills

`.factory/bin/mfactory skills` lists every skill the factory can attach, in
three groups, and the exact string to put in `skills`. Nothing is read unless
you list it. The same list works on `[skills].all` (every agent), a step, and
a reviewer.

```toml
[skills]
all = ["code-style"]                 # every agent

[steps.build]
skills = ["testing", "user:python-best-practices", "superpowers:test-driven-development"]

[reviewers.security]
skills = ["auth-rules"]

[steps.spec]
skills = ["all"]                     # every project, user and plugin skill on this machine
```

| Entry | Where it comes from | What the agent does |
| --- | --- | --- |
| `testing` | `.agents/skills`, `.claude/skills`, `.cursor/skills`, `.codex/skills` in the repo, first folder wins | Reads that `SKILL.md` in the main checkout. Claude Code also preloads a skill that lives in `.claude/skills`. |
| `user:python-best-practices` | `~/.agents/skills`, `~/.claude/skills`, `~/.cursor/skills`, `~/.codex/skills`, first folder wins | Reads the `~/.../SKILL.md` path written at sync time. |
| `superpowers:test-driven-development` | An installed plugin. The text before the first colon is the plugin. | Claude Code invokes it with the Skill tool. Cursor uses it by name. Codex follows `$superpowers:test-driven-development`. When that tool has the plugin's files, the prompt also gives their `~/` path. |
| `all` | Every project skill, user skill, and plugin skill the enabled tools can see right now | Same as listing each of them. A plugin skill is written into a tool's agent only when that tool has it. |

A project skill that matches no file fails `mfactory sync`, and the error names
the key. A user skill or plugin skill is per machine: sync still writes the
agent files and prints a note, and `mfactory doctor` warns, naming the tool
that cannot see a plugin skill. Run sync again on a machine that has the skill
and the prompt gains its path.

Codex counts a plugin when `~/.codex/config.toml` enables `plugins."<plugin>@..."`.
Claude prefers the install recorded in `~/.claude/plugins/installed_plugins.json`
when that file names the plugin. Cursor uses its plugin cache.

## Instruction files

`skills = ["all"]` does not attach rules, `AGENTS.md`, or `CLAUDE.md`. Those
go in `rules`. `[rules].all` is read by every agent. A step's or reviewer's
`rules` is extra. `.factory/bin/mfactory skills` lists them under
`instructions`.

This is the default. Every agent reads every instruction file found. The builder
also names `AGENTS.md` and `~/.claude/CLAUDE.md`, so a missing `AGENTS.md` fails
sync and a missing user file is a doctor warning.

```toml
[rules]
all = ["all"]

[steps.build]
rules = ["AGENTS.md", "user:CLAUDE.md"]
```

| Entry | Where it comes from |
| --- | --- |
| `all` | Every file below that exists now. |
| `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md` | That file at the repository root. |
| `.claude/CLAUDE.md` | That file. |
| `.claude/rules/...md`, `.cursor/rules/...md` or `.mdc`, `.agents/rules/...md` | That rule file. |
| `user:CLAUDE.md` | `~/.claude/CLAUDE.md`. |
| `user:.codex/AGENTS.md`, `user:.agents/AGENTS.md` | That home file. |
| `user:.claude/rules/...`, `user:.cursor/rules/...` | That home rule file. |

`all` looks in the repository first: `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`,
`.claude/CLAUDE.md`, then `.claude/rules`, `.cursor/rules`, and `.agents/rules`.
Then the home directory: `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`,
`~/.agents/AGENTS.md`, `~/.claude/rules`, and `~/.cursor/rules`.

The agent's prompt lists each path. When that section is present, it is the
set to follow, so a repository with both `AGENTS.md` and `CLAUDE.md` follows
both. When `rules` is empty, the agent still reads `AGENTS.md`, or `CLAUDE.md`
when `AGENTS.md` is missing.

A missing project file fails `mfactory sync`. A missing user file syncs, and
`mfactory doctor` warns. `SKILL.md` files and plugin caches are not rule files.

## Labels, paths and ports

```toml
[labels]
queue = "factory"
needs_info = "factory:needs-info"
needs_human = "factory:needs-human"

[paths]
worktrees = ".worktrees"
protected = [".github/workflows/*", "infra/*"]

[ports]
base = 5100
step = 10
```

| Key | Meaning |
| --- | --- |
| `[labels] queue` | Issues with this label are built. |
| `[labels] needs_info` | Put on an issue while its author is being asked. |
| `[labels] needs_human` | Put on an issue while a maintainer, the operator, or a named login is being asked. |
| `[paths] worktrees` | Where each job's worktree goes, relative to the repository. Keep it ignored by git. |
| `[paths] protected` | Patterns of files no job may change or delete. `*` also matches `/`. A job may add new files that match. |
| `[ports] base`, `step` | Job number `n` gets ports from `base + step * (n % 100)`, so dev servers of parallel jobs do not collide. |

No job may add, change or delete `factory.toml`, anything under `.factory/`, or
a generated `mfactory-*` agent or skill file, whatever `protected` says. This
stops a job from rewriting the pipeline, the checks, or the agents that judge
it.
