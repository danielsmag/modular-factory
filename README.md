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

![The dashboard's board view, showing made-up sample jobs](docs/dashboard-board.png?v=2)

> **Status: 0.2.1, not yet run inside a real tool.** The pipeline is covered by
> 179 automated tests that play the orchestrator and the agents against a
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

## The pipeline is a graph you can read and change

Each step names its type and where each outcome leads. This is the default
pipeline, printed by `.factory/bin/mfactory graph`:

```text
intake          agent   ACCEPT -> spec | NEEDS-INFO -> ask-author (max 2) | REJECT -> rejected
ask-author      human   ANSWER -> intake | REJECT -> rejected
spec            agent   DONE -> spec-approval
spec-approval   human   off, continues as APPROVE -> build
build           agent   READY -> gate | BLOCKED -> escalated
gate            checks  PASS -> review | FAIL -> debug (max 2)
debug           agent   off, continues as FOUND -> build
review          review  PASS -> approve | CHANGES -> build (max 2)
approve         agent   APPROVE -> ready-to-merge | ESCALATE -> escalated
ready-to-merge  human   FEEDBACK -> build (max 3) | REJECT -> rejected
merge           merge   off, continues as MERGED -> verify
verify          verify  off, continues as PASS -> done
escalated       human   FEEDBACK -> build | REJECT -> rejected
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

Three other steps ship switched off. Turning one on is two lines in `factory.toml`:

```toml
[steps.spec-approval]     # a maintainer approves the spec before anything is built
enabled = true

[steps.debug]             # a debugger diagnoses failing checks before the builder retries
enabled = true

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

Agents read the skills you already keep in `.claude/skills`, `.cursor/skills`,
`.agents/skills` or `.codex/skills`. You say which:

```toml
[skills]
all = ["code-style"]          # every agent

[steps.build]
skills = ["testing"]          # only the builder
```

## Dashboard

```bash
.factory/bin/mfactory dashboard
```

It serves a read-only page on your machine: every job and its current step, the
pipeline with the number of jobs in each step, what each job wrote, and the
merged configuration with the source of each value. It changes nothing;
approvals and feedback go through `/mfactory` or an issue comment.

![The dashboard's pipeline view, showing made-up sample jobs](docs/dashboard-pipeline.png?v=2)

## Several projects

Install the factory in each project, then subscribe that checkout. The list lives in `~/.mfactory/fleet.toml`.

```bash
cd /path/to/project-a && mfactory fleet subscribe
cd /path/to/project-b && mfactory fleet subscribe
mfactory fleet dashboard
```

`mfactory dashboard` and `mfactory fleet dashboard` open the same page. It lists every subscribed project, and a filter can show one of them. Inside a subscribed checkout, `mfactory dashboard` opens with that project selected. Outside a repository, or with `--all`, it opens on every project. A checkout that is not subscribed still has its own page. `mfactory status` outside a repository shows every subscribed project. `mfactory upgrade --all` upgrades each one. Each job stays in its own checkout. Open that project and run `/mfactory run` there. The [install guide](docs/install.md#several-projects) has the rest.

## Commands

```text
mfactory install | upgrade | uninstall | doctor | status | sync
mfactory config show | graph | models | skills     see the configuration
mfactory queue | create | next | done | answer     run jobs (the /mfactory skill uses these)
mfactory summary | checks | dashboard | start dashboard | stop | version
mfactory fleet subscribe | unsubscribe | list | queue | dashboard
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
