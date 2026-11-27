# modular-factory

**[Documentation](https://danielsmag.github.io/modular-factory/docs/)**

A software factory you install into any repository. You label a GitHub issue;
agents decide whether it is worth building, write a spec, build it on a branch
and review it. By default a maintainer merges the pull request. A project can
turn the merge step on so the factory merges it. It runs inside the coding tool
you already use: Claude Code, Cursor or Codex.

Everything a project changes lives in one file, `factory.toml`: the pipeline's
steps, the commands that prove a build works, which model and effort each step
uses, how often a step may send work back, and which of the project's own
skills each agent reads.

![The dashboard's board view: Jira boards, GitHub issues and the factory's jobs, all made up](docs/dashboard-board.png?v=3)

> **Status: not yet run inside a real tool.** The pipeline is covered by
> automated tests that play the orchestrator and the agents against a
> stand-in for GitHub. Nobody has yet run a job end to end in Claude Code,
> Cursor or Codex. Expect to find problems on the first real run; see
> [what is untested](docs/design.md#what-is-untested).

## Install

Install the `mfactory` command once:

```bash
curl -fsSL https://raw.githubusercontent.com/danielsmag/modular-factory/master/install.sh | bash -s -- --global
```

Then, in each repository:

```bash
mfactory install --tool claude
```

Use `--tool cursor` or `--tool codex` instead, or repeat `--tool` for several.
You need Python 3.11 or later, `git`, and the GitHub CLI `gh`, signed in.

To install straight into one repository, without the command on your `PATH`:

```bash
curl -fsSL https://raw.githubusercontent.com/danielsmag/modular-factory/master/install.sh | bash -s -- --tool claude
```

Then, in your coding tool:

```text
/mfactory setup
```

This reads the project and fills in `factory.toml`: what the project is for and
the commands that install, lint and test it. Commit `factory.toml`, `.factory/`
and the generated agent files to your base branch, and push. The
[install guide](docs/install.md) has each step and what to do when
`mfactory doctor` reports a problem.

## Use

```text
/mfactory run                  work through every issue labeled `factory`
/mfactory run 12               run issue 12 now
/mfactory approve 12           answer a job that is waiting for you
/mfactory <anything else>      ask about jobs, or describe a feature to file as an issue
```

A job that needs a person stops and says so on the issue or the pull request.
You answer there with a comment, such as `/mfactory changes Use the existing
Money type`, or in your tool. Nothing else waits for it: other jobs keep
running.

Jobs can come from a Jira kanban board instead of, or as well as, GitHub
issues: the board's queue column is the queue, every step's card sits in a
column, and people reply in Jira comments or by moving the card. With
`kind = ["github", "jira"]` both queues feed one project, and each job talks
on its own tracker. See
[Tracker: a Jira board](docs/configuration.md#tracker-a-jira-board).

## The pipeline is a graph you can read and change

Each step names its type and where each outcome leads. This is the default
pipeline, printed by `.factory/bin/mfactory graph`:

```text
intake           agent   ACCEPT -> spec | NEEDS-INFO -> ask-author (max 2) | REJECT -> rejected
ask-author       human   ANSWER -> intake | REJECT -> rejected
spec             agent   DONE -> spec-approval
spec-approval    human   off, continues as APPROVE -> build
build            agent   READY -> gate | BLOCKED -> escalated
gate             checks  PASS -> review | FAIL -> debug (max 2)
debug            agent   off, continues as FOUND -> build
review           review  PASS -> approve | CHANGES -> build (max 2)
approve          agent   APPROVE -> ready-to-merge | ESCALATE -> escalated
ready-to-merge   human   FEEDBACK -> build (max 3) | REJECT -> rejected
merge            merge   off, continues as MERGED -> ready-to-verify
ready-to-verify  human   off, continues as VERIFY -> verify
verify           verify  off, continues as PASS -> done
escalated        human   FEEDBACK -> build | REJECT -> rejected
```

The factory does not merge unless the project turns that step on. All three
keys are required. Enabling `merge` alone leaves it unreachable, and leaving
`ready-to-merge` on after retargeting `approve` does the same:

```toml
[steps.approve]
on = { APPROVE = "merge" }

[steps.ready-to-merge]
enabled = false
skip = "REJECT"

[steps.merge]
enabled = true
```

Do that only when a merge to the base branch does not publish a release.
With the default, approving marks the pull request ready and waits. Merging
it by hand continues from the merge step's `MERGED` edge. That path does not
sync the base branch or run the checks again.

Four other steps ship switched off. Turning one on is two lines in `factory.toml`:

```toml
[steps.spec-approval]     # a maintainer approves the spec before anything is built
enabled = true

[steps.debug]             # a debugger diagnoses failing checks before the builder retries
enabled = true

[steps.ready-to-verify]   # wait until a person says the deploy is live
enabled = true
who = "operator"

[steps.verify]            # check the change again after it is merged or deployed
enabled = true
commands = ["npm run test:smoke"]
```

Adding your own step is a block and one changed edge:

```toml
[steps.docs]
type = "agent"
instructions = "Update docs/ for the change on this branch."
on = { DONE = "gate" }

[steps.build]
on = { READY = "docs" }   # build now hands over to docs, which hands over to the checks
```

A `max` on an edge is a loop limit: how many times in a row a step may send a
job that way. When it is used up, the job goes to a maintainer instead of going
round again.

```toml
[steps.review]
on = { CHANGES = { max = 2 } }    # review may ask for changes twice
```

### After the merge: close, test or debug

A merged pull request can still turn out wrong: a smoke test fails, or CI
gets twice as slow. This recipe asks a maintainer what to do once the pull
request merges. They answer on the issue with `/mfactory close`,
`/mfactory test` or `/mfactory debug <what you saw>`:

```toml
[project]
issue_link = "refs"   # the default: the issue stays open, so the follow-up PR links it

[steps.merge]
on = { MERGED = "after-merge", FAILED = "escalated" }

[steps.after-merge]
type = "human"
who = "maintainer"
ask = "merged: close the job, run the post-merge tests, or debug"
on = { CLOSE = "done", TEST = "verify", DEBUG = "post-debug" }

[steps.verify]
enabled = true
after = "merge"
commands = ["pytest -q"]
on = { PASS = "after-merge", FAIL = "post-debug", BLOCKED = { to = "after-merge", max = 3 } }

[steps.post-debug]
type = "agent"
on = { FOUND = { to = "build", max = 2 }, UNKNOWN = "after-merge" }
```

`post-debug` is a built-in role. Its agent works in a fresh checkout of the
base branch, reads CI and the merged change, and writes
`round-N/post-debug.md` without changing any code. On `FOUND` the same job
builds a fix on a new branch, `factory/<job>-fix-1`. That branch has a new
pull request that refs the same issue, and the fix goes through the checks,
review and approval again. The dashboard shows both pull requests. The `max`
on `FOUND` limits how many follow-ups one job may open.

## Models and effort per step

```toml
[models.claude]
build = { model = "opus", effort = "high" }
reviewer-code = { model = "sonnet", effort = "medium", low = { model = "haiku", effort = "low" } }

[models.cursor]
default = { model = "gpt-5.6-sol", effort = "medium" }   # every step that names no model of its own
```

`low`, `medium` and `high` are the job's risk, which intake decides: the
example reviews low-risk jobs with a cheaper model. Run
`.factory/bin/mfactory models` to see what every agent uses in every tool.

## Your project's skills

Agents read the skills you list. A bare name is a project skill in
`.claude/skills`, `.cursor/skills`, `.agents/skills` or `.codex/skills`.
`user:name` is a skill in your home directory. `plugin:skill` is an installed
plugin skill. `all` takes every one of those.

```toml
[skills]
all = ["code-style"]          # every agent

[steps.build]
skills = ["testing", "user:python-best-practices", "superpowers:test-driven-development"]

[steps.spec]
skills = ["all"]              # every project, user and plugin skill on this machine

[rules]
all = ["all"]                 # default: every AGENTS.md, CLAUDE.md, and rule file found

# builder default: AGENTS.md (or CLAUDE.md if missing) and ~/.claude/CLAUDE.md
# [steps.build]
# rules = ["AGENTS.md", "user:CLAUDE.md"]
```

## Dashboard

```bash
mfactory start dashboard
mfactory stop
```

The page comes with the global install (`install.sh --global`), not with each
project. `.factory/bin/mfactory dashboard` hands off to the global `mfactory`.
It serves a read-only page on your machine. Its Board has three blocks:

- **Jira**: the Jira boards you choose, one or several, each drawn with its
  columns and cards. A picker lists every board your Jira account can see; the
  project's own board is shown until you choose.
- **GitHub issues**: each repository's open issues, newest first, with All,
  Assigned to me and Not in the factory.
- **Factory**: jobs by step, with a Now panel of the agents still on the
  current step and the queued issues that are not jobs yet.

A card or an issue that is a factory job carries the job's chip. Each block
folds, and the browser remembers the boards you chose and what you folded. The
page also has the pipeline, what each job wrote, an overview with the job
table, and the merged configuration with the source of each value. An issue
opens its body and comments. It changes nothing; approvals and feedback go
through `/mfactory` or an issue comment. The Jira block reads Jira's REST API,
so start the dashboard from a shell that has `JIRA_EMAIL` and `JIRA_API_TOKEN`.

![The dashboard's pipeline view, showing made-up sample jobs](docs/dashboard-pipeline.png?v=2)

## Several projects

`mfactory install` adds each project to your workspace, `~/.mfactory/workspace.toml`. A checkout that was installed before can be added by hand.

```bash
cd /path/to/project-a && mfactory workspace add
mfactory workspace list
mfactory start dashboard
```

The dashboard lists every project in the workspace, and a filter can show one of them. Inside a listed checkout, it opens with that project selected. A checkout that is not listed still opens its own page. A project whose job files were written by an older factory shows a note to upgrade it, and its jobs still show. `mfactory status` outside a repository shows every project in the workspace. `mfactory upgrade` upgrades the global CLI and each of those projects. Each job stays in its own checkout. Open that project and run `/mfactory run` there. The [install guide](docs/install.md#several-projects) has the rest.

## Commands

```text
mfactory install | upgrade | uninstall | doctor | status | sync
mfactory config show | graph | models | skills     see the configuration
mfactory queue | create | next | done | answer     run jobs (the /mfactory skill uses these)
mfactory checks | dashboard | start dashboard | stop | version
mfactory workspace add | remove | list | queue | init
```

Run them as `.factory/bin/mfactory <command>` from anywhere in the repository.

## Documentation

**[Read the docs](https://danielsmag.github.io/modular-factory/docs/).** Sidebar,
search, and a page for each guide, in the same look as the dashboard. The same
material is in the repository:

- [Install guide](docs/install.md): installing, setting up, upgrading, removing.
- [Configuration reference](docs/configuration.md): every key in `factory.toml`.
- [Design notes](docs/design.md): how it works, what guards it, what is
  untested, and what changed from the project it is derived from.

## Development

```bash
python3 -m pip install pytest ruff   # skip when .venv already has them
make check                 # ruff, bash -n install.sh, pytest
make help                  # mfactory CLI targets
make sample-dashboard      # the dashboard with made-up jobs
```

The factory itself uses only Python's standard library.

## Licence

GPLv3. See [NOTICE](NOTICE) and [what changed](docs/design.md#what-changed-from-the-reference).
