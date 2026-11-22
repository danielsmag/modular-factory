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
.factory/bin/mfactory skills        # the project's skills and who reads them
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
| `schema` | `1` | The version of this file's format. `mfactory upgrade` refuses a release that needs another. |
| `[project] description` | `""` | What the project is for and what does not belong in it. Intake judges issues against it. |
| `[project] base_branch` | `"master"` | Jobs branch from `origin/<base_branch>` and merge into it. |
| `[project] merge_method` | `"squash"` | `merge`, `squash` or `rebase`. With `squash`, the pull request title becomes the commit on the base branch. |
| `[project] pr_title` | `""` | Example pull request title, such as `feat: add CSV export [ABC-123]`. The builder uses this shape instead of the spec title. Empty means the spec title. The script does not fill placeholders. |
| `[project] pr_title_pattern` | `""` | Regular expression the whole title must match (`fullmatch`). Empty means no check. `done` refuses a `READY` build whose title does not match. |
| `[tools] enabled` | `["claude"]` | The tools to write agent files for: `claude`, `cursor`, `codex`. |
| `[tools.claude] stop_hook` | `false` | Run the checks when the build agent stops, and send it back to work if they fail. Claude Code only. |
| `[tools.claude] cost` | `true` | Work out each job's cost from Claude Code's transcripts. Other tools record no cost. |
| `[tools.codex] intake_shell` | `false` | Let the Codex intake agent run commands. See [Design notes](design.md#intake-has-no-shell). |
| `[run] concurrency` | `3` | Jobs this project works on at once. A job waiting for a person, or being validated after merge, does not count. |
| `[fleet] concurrency` | `3` | `new` and `resume` rows kept across every project when this factory runs `fleet queue`. `fleet.toml` or `--concurrency` overrides it. A project's `factory.toml` cannot. |
| `[run] start` | `"intake"` | The step a new job starts in. |
| `[run] escalate_to` | `"escalated"` | The human step a job goes to when a loop limit is reached. |

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

## Checks

```toml
[checks]
commands = ["npm ci --prefer-offline", "npm run lint", "npm test -- --run"]
timeout_minutes = 30
```

The commands run in order in the job's worktree with `bash -c`. The first one
that exits non-zero stops the run and fails the step. They run at the `gate`
step, and again in the `merge` step after the base branch is merged in, when
that step is on.

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
| `verify` | The script, and optionally an agent, check the merged change. | `PASS`, `FAIL` |

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

`spec-approval`, `debug` and `verify` ship with `enabled = false`. The merge
step does too; turning that one on is the block above, not a single key.

```toml
[steps.spec-approval]     # a maintainer approves spec.md before anything is built
enabled = true

[steps.debug]             # after failing checks, a debugger finds the cause before the builder retries
enabled = true

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
`on`:

```toml
[steps.license-check]
type = "agent"
instructions = "List every new dependency and its licence. FLAG if any is not MIT, BSD or Apache-2.0."
on = { CLEAR = "review", FLAG = "escalated" }
```

Keys of an `agent` step:

| Key | Meaning |
| --- | --- |
| `instructions` | What the agent must do. Required for a step you add. |
| `writes` | The file it writes in the job folder. Default: `<step>.md`. |
| `context` | Project facts added to the agent's prompt, such as where things live. Works on built-in steps too. |
| `skills` | Project skills this agent reads. |
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
| `who` | `"maintainer"` | Who may reply. `author`: the issue's author. `maintainer`: anyone with write access to the repository. |
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

The word after `/mfactory` is the outcome's name in lower case, so a step with
`on = { SHIP = "merge", HOLD = "build" }` takes `/mfactory ship` and
`/mfactory hold <what to change>`.

- Words given with a reply are saved as `feedback-<k>.md` in the job folder,
  and the next agent is told to act on them.
- `REJECT` ends the job as `rejected`. It needs a reason.
- On a step with an `ANSWER` outcome, such as `ask-author`, any comment from
  the author counts as the answer.
- Comments from anyone else are ignored.
- Merging the job's pull request by hand continues the job from the merge
  step's `MERGED` edge, including when that step is off. On the default path
  this does not sync the base branch or run the checks again.

## Validation after merge or deploy

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
| `agent` | `false` | After the commands pass, a verifier agent checks the spec's acceptance criteria against the running result and writes `verify.md`. |
| `url` | `""` | Where the deployed result is, passed to the commands and the agent. |
| `retries` | `1` | How many times to run failing commands again before the step fails. |
| `on_failure` | `"comment"` | What to do when validation fails, below. |

The commands and `wait` get these environment variables: `FACTORY_JOB`,
`FACTORY_MERGE_SHA`, `FACTORY_PR` and `FACTORY_DEPLOY_URL`.

A minimal `wait` script:

```bash
#!/usr/bin/env bash
# Exits 0 once the deployed version is the merge commit.
test "$(curl -fsS "$FACTORY_DEPLOY_URL/version")" = "$FACTORY_MERGE_SHA"
```

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
`security` surface; `ux` and `ui` on jobs with the `ui` surface.

A new reviewer is a table with a `focus`. No prompt file is needed:

```toml
[reviewers.migrations]
focus = "Database migrations: reversibility, locks on large tables, data loss."

[surfaces.database]
when = "it adds or changes a migration or a table"
reviewers = ["migrations"]
```

A surface is something a change can touch. Intake reads each `when` and names
the surfaces a job touches; the job then gets those surfaces' reviewers. A
reviewer that no surface lists runs on every job.

To add a reviewer to a surface that exists, restate the list:

```toml
[reviewers.a11y]
focus = "Keyboard access, focus order, labels and contrast of anything a user sees."
port = true

[surfaces.ui]
reviewers = ["ux", "ui", "a11y"]
```

| Key | Meaning |
| --- | --- |
| `[reviewers.<name>] focus` | What this reviewer looks for. Required for a reviewer you add. |
| `[reviewers.<name>] context` | Project facts added to its prompt. |
| `[reviewers.<name>] skills` | Project skills it reads. |
| `[reviewers.<name>] port` | `true` gives it a port for a dev server, for reviewers that look at the running app. |
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

The factory finds skills in `.claude/skills`, `.cursor/skills`,
`.agents/skills` and `.codex/skills`. `.factory/bin/mfactory skills` lists them.
Nothing is read unless you list it:

```toml
[skills]
all = ["code-style"]                 # every agent

[steps.build]
skills = ["testing", "migrations"]   # the builder as well reads these

[reviewers.security]
skills = ["auth-rules"]
```

Each agent's prompt then names the `SKILL.md` files to read before it starts.
This works the same in every tool, whether or not the tool would load the
skill by itself. In Claude Code, a skill that lives in `.claude/skills` is also
loaded into the agent up front.

`mfactory sync` refuses a name that matches no skill and says where it looked.

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
| `[labels] needs_human` | Put on an issue while a maintainer is being asked. |
| `[paths] worktrees` | Where each job's worktree goes, relative to the repository. Keep it ignored by git. |
| `[paths] protected` | Patterns of files no job may change or delete. `*` also matches `/`. A job may add new files that match. |
| `[ports] base`, `step` | Job number `n` gets ports from `base + step * (n % 100)`, so dev servers of parallel jobs do not collide. |

No job may add, change or delete `factory.toml`, anything under `.factory/`, or
a generated `mfactory-*` agent or skill file, whatever `protected` says. This
stops a job from rewriting the pipeline, the checks, or the agents that judge
it.
