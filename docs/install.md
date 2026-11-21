# Install guide

## What you need

- A git repository with a GitHub remote named `origin`.
- Python 3.11 or later as `python3`.
- `git`, and the GitHub CLI `gh`, signed in (`gh auth login`) with write access
  to the repository.
- One of Claude Code, Cursor or Codex.

The factory adds nothing to your project's dependencies. It is a folder of
Python files that use only the standard library.

## 1. Install

Install the command once, for your user:

```bash
curl -fsSL https://raw.githubusercontent.com/danielsmag/modular-factory/master/install.sh | bash -s -- --global
```

| Option | Meaning |
| --- | --- |
| `--global` | Install `mfactory` for your user. It must not be combined with `--tool`. |
| `--version v0.1.0` | Install that release. Default: the latest release, or the `master` branch before the first release. |
| `--source DIR` | Install from a local copy of this repository instead of downloading. |

This creates `~/.mfactory` (`FACTORY_HOME` when set) and links `~/.local/bin/mfactory`. The fleet list is `~/.mfactory/fleet.toml`. Then, in each repository:

```bash
mfactory install --tool claude
```

To install straight into the repository you are in, without a command on `PATH`:

```bash
curl -fsSL https://raw.githubusercontent.com/danielsmag/modular-factory/master/install.sh | bash -s -- --tool claude
```

| Option | Meaning |
| --- | --- |
| `--tool claude\|cursor\|codex` | The tool to generate agents for. Repeat it or separate names with commas for several. Default: `claude`. |
| `--version v0.1.0` | Install that release. Default: the latest release, or the `master` branch before the first release. |
| `--source DIR` | Install from a local copy of this repository instead of downloading. |

To read the script before running it, download it first:

```bash
curl -fsSL -o install.sh https://raw.githubusercontent.com/danielsmag/modular-factory/master/install.sh
less install.sh
bash install.sh --tool cursor
```

It does five things and prints each:

```text
ok    .factory/ installed (version 0.1.0)
ok    factory.toml written
ok    .gitignore gained /.factory/jobs/, /.worktrees/
ok    generated files for claude: 10 written
ok    GitHub labels: factory, factory:needs-info, factory:needs-human
```

| Path | What it is | Commit it? |
| --- | --- | --- |
| `factory.toml` | Your configuration. The only file you edit. | Yes |
| `.factory/` | The mfactory: its CLI, prompts and defaults. | Yes |
| `.factory/jobs/` | The state and files of each job. | No (ignored) |
| `.worktrees/` | One git worktree per running job. | No (ignored) |
| `.claude/agents/mfactory-*.md`, `.cursor/agents/mfactory-*.md`, `.codex/agents/mfactory-*.toml` | One small agent file per step, generated from `factory.toml`. | Yes |
| `.claude/skills/mfactory/` or `.agents/skills/mfactory/` | The `/mfactory` skill. | Yes |

Running the installer again is safe: it never overwrites `factory.toml`.

## 2. Set up

In your coding tool, in the repository:

```text
/mfactory setup
```

In Codex, mention the skill with `$factory setup`, or ask in words: "set up the
factory".

The agent reads your README, package manifest and CI workflows, then edits
`factory.toml`:

- `[project] description`: what the project is for. Intake judges issues
  against this.
- `[checks] commands`: the commands that install dependencies and run lint,
  tests and the build. These decide whether a build may be reviewed and merged.
- `[skills]`: which of your project's skills agents read.

It asks before turning on any optional step. By default a maintainer merges
the pull request and the factory does not. Ask whether a merge to the base
branch publishes a release. Only when it does not, and the user wants the
factory to merge, turn the merge step on, point `approve` at it, and switch
`ready-to-merge` off. You can
also edit `factory.toml` by hand; the [configuration reference](configuration.md)
lists every key.

After any change to `factory.toml`:

```bash
.factory/bin/mfactory sync
```

This rewrites the generated agent files to match.

## 3. Check

```bash
.factory/bin/mfactory doctor
```

```text
ok    factory 0.1.0, Python 3.12.3
ok    git: git version 2.43.0
ok    gh: gh version 2.60.0
ok    gh is signed in
ok    factory.toml is valid: 9 steps on, off: spec-approval, debug, merge, verify
ok    Claude Code: claude 2.1.0
ok    generated agent and skill files match factory.toml
ok    origin/main can be fetched
ok    .factory/jobs/ is ignored by git
ok    GitHub labels exist
...   running the checks on origin/main
ok    the checks pass on origin/main (41s)

Everything a run needs is in place.
```

The version numbers above are examples.

It runs your checks on a clean copy of the base branch, which can take as long
as your CI. Pass `--skip-checks` to leave that out.

| It reports | What to do |
| --- | --- |
| A problem in `factory.toml`, naming the key | Fix that key. `mfactory graph` shows the pipeline in effect. |
| `[checks].commands is empty` | Run `/mfactory setup`, or list the commands yourself. |
| Generated files differ from `factory.toml` | Run `.factory/bin/mfactory sync`. Edit `factory.toml`, never the generated files. |
| `factory.toml and .factory/ aren't on origin/<base> yet` | Commit and push them. Jobs build from that branch. |
| The checks fail on the base branch | Fix the base branch first. Otherwise every job fails for a reason it did not cause. |
| A label is missing | `mfactory init` creates them; it needs write access to the repository. |
| `<tool> isn't on PATH` | A warning only. It is fine if you run the tool as an app. |

## 4. Commit and run

```bash
git add factory.toml .factory .gitignore .claude .cursor .codex .agents 2>/dev/null
git commit -m "Add the software factory"
git push
```

Then label an issue `factory` and, in your tool:

```text
/mfactory run
```

Only people with triage or write access can add a label, so the label decides
whose issues the factory builds.

To watch the jobs:

```bash
.factory/bin/mfactory dashboard
mfactory start dashboard
mfactory stop
```

`dashboard` stays in the foreground until Ctrl C. `start dashboard` serves the same page and returns. `stop` ends that process.

## Several projects

The factory in a project only sees that project. Your fleet file is `~/.mfactory/fleet.toml`. Subscribe from inside a project that already has the factory:

```bash
cd /path/to/project-a
mfactory fleet subscribe
cd /path/to/project-b
mfactory fleet subscribe
mfactory fleet list
mfactory fleet queue
mfactory fleet dashboard
mfactory fleet unsubscribe
```

`subscribe` and `unsubscribe` take a path when you are not standing in the repository. `mfactory dashboard` outside a repository, or in a checkout with no `factory.toml`, uses this same list. `mfactory status` outside a repository does too. `mfactory upgrade --all` upgrades every subscribed project. A project that fails does not stop the others, and each repository still needs its own commit.

A `fleet.toml` in the current directory or a parent still wins, and so does `FACTORY_FLEET`. `mfactory fleet init` writes that local file. `fleet add` accepts a checkout only after the factory is installed there.

`fleet queue` runs each project's own `.factory/bin/mfactory queue`. A project that fails is reported, and the others still print. `fleet dashboard` serves the same read-only dashboard as `mfactory dashboard`, with a filter for each project. Choose All for one board, one pipeline per project, one job list, and one config table per project. Choose one project for that project's board, pipeline, jobs, and config. Queue rows that are not jobs yet stay on the board.

Each project caps its own jobs with `[run].concurrency` (default 3). The fleet has a second cap, `[fleet].concurrency` in `defaults.toml` (also 3): that many `new` and `resume` rows across every project. `concurrency` in `fleet.toml`, or `mfactory fleet queue --concurrency N`, overrides the fleet cap. Later rows print as `hold`. Projects earlier in the file get the slots. `wait`, `skip` and `hold` do not take a slot. A project's `factory.toml` cannot set the fleet cap.

Building stays in the project. Open that project and run `/mfactory run`, or `/mfactory run 12` for one issue. Inside a subscribed project, `mfactory dashboard` opens this same page with that project selected. A project that is not in the fleet still opens only its own page.

Two clones of one GitHub repository both appear if you add both paths. They do not share job files.

## Permissions your tool will ask for

The orchestrator runs `.factory/bin/mfactory`. The agents it starts run `git`,
`gh` and your project's check commands inside the job's worktree. If your tool
asks before each command, allow these for the repository, or a run stops at
every step waiting for you.

## Using more than one tool

```toml
[tools]
enabled = ["claude", "cursor"]
```

`mfactory sync` then writes agent files for each. Any one of the tools can run
`/mfactory run`; they share the same job state in `.factory/jobs/`, so do not
run two orchestrators on the same checkout at the same time.

Cursor also reads `.claude/skills`. With Claude Code and Cursor enabled, the
factory writes the skill only to `.claude/skills/mfactory`, so Cursor lists it
once. With all three tools enabled it also writes `.agents/skills/mfactory` for
Codex, and Cursor may list the skill twice; the copies are identical.

## Upgrade

```bash
.factory/bin/mfactory upgrade                 # the latest release
.factory/bin/mfactory upgrade --version v0.2.0
```

It replaces `.factory/` (not `.factory/jobs/`), runs `sync`, and leaves
`factory.toml` alone. It refuses a release whose `schema` number differs from
your `factory.toml`; the [changelog](../CHANGELOG.md) says what to change when
that number moves. Commit the result.

## Remove

```bash
rm -rf .factory factory.toml .worktrees
rm -rf .claude/agents/mfactory-*.md .cursor/agents/mfactory-*.md .codex/agents/mfactory-*.toml
rm -rf .claude/skills/mfactory .agents/skills/mfactory
git worktree prune
```

Then delete the three `factory` labels on GitHub if you no longer want them.
