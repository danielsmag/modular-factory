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

Run this in the repository:

```bash
curl -fsSL https://raw.githubusercontent.com/danielsmag/modular-factory/main/install.sh | bash -s -- --tool claude
```

| Option | Meaning |
| --- | --- |
| `--tool claude\|cursor\|codex` | The tool to generate agents for. Repeat it or separate names with commas for several. Default: `claude`. |
| `--version v0.1.0` | Install that release. Default: the latest release, or the `main` branch before the first release. |
| `--source DIR` | Install from a local copy of this repository instead of downloading. |

To read the script before running it, download it first:

```bash
curl -fsSL -o install.sh https://raw.githubusercontent.com/danielsmag/modular-factory/main/install.sh
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
| `.factory/` | The factory: its CLI, prompts and defaults. | Yes |
| `.factory/jobs/` | The state and files of each job. | No (ignored) |
| `.worktrees/` | One git worktree per running job. | No (ignored) |
| `.claude/agents/factory-*.md`, `.cursor/agents/factory-*.md`, `.codex/agents/factory-*.toml` | One small agent file per step, generated from `factory.toml`. | Yes |
| `.claude/skills/factory/` or `.agents/skills/factory/` | The `/factory` skill. | Yes |

Running the installer again is safe: it never overwrites `factory.toml`.

## 2. Set up

In your coding tool, in the repository:

```text
/factory setup
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

It asks before turning on any optional step. You can also edit `factory.toml`
by hand; the [configuration reference](configuration.md) lists every key.

After any change to `factory.toml`:

```bash
.factory/bin/factory sync
```

This rewrites the generated agent files to match.

## 3. Check

```bash
.factory/bin/factory doctor
```

```text
ok    factory 0.1.0, Python 3.12.3
ok    git: git version 2.43.0
ok    gh: gh version 2.60.0
ok    gh is signed in
ok    factory.toml is valid: 9 steps on, off: spec-approval, debug, verify
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
| A problem in `factory.toml`, naming the key | Fix that key. `factory graph` shows the pipeline in effect. |
| `[checks].commands is empty` | Run `/factory setup`, or list the commands yourself. |
| Generated files differ from `factory.toml` | Run `.factory/bin/factory sync`. Edit `factory.toml`, never the generated files. |
| `factory.toml and .factory/ aren't on origin/<base> yet` | Commit and push them. Jobs build from that branch. |
| The checks fail on the base branch | Fix the base branch first. Otherwise every job fails for a reason it did not cause. |
| A label is missing | `factory init` creates them; it needs write access to the repository. |
| `<tool> isn't on PATH` | A warning only. It is fine if you run the tool as an app. |

## 4. Commit and run

```bash
git add factory.toml .factory .gitignore .claude .cursor .codex .agents 2>/dev/null
git commit -m "Add the software factory"
git push
```

Then label an issue `factory` and, in your tool:

```text
/factory run
```

Only people with triage or write access can add a label, so the label decides
whose issues the factory builds.

To watch the jobs:

```bash
.factory/bin/factory dashboard
```

## Several projects

The factory in a project only sees that project. To watch several, keep a fleet file in a directory of your own. That directory is not one of the projects, and you do not commit it into them.

```bash
mkdir -p ~/work/fleet
cd ~/work/fleet
/path/to/modular-factory/core/bin/factory fleet init
/path/to/modular-factory/core/bin/factory fleet add /path/to/project-a
/path/to/modular-factory/core/bin/factory fleet add /path/to/project-b
/path/to/modular-factory/core/bin/factory fleet queue
/path/to/modular-factory/core/bin/factory fleet dashboard
```

`fleet add` accepts a checkout only after the factory is installed there. After you upgrade a project, that project's `.factory/bin/factory` is the same command. Run it from the fleet directory:

```bash
cd ~/work/fleet
/path/to/project-a/.factory/bin/factory fleet queue
```

`fleet queue` runs each project's own `.factory/bin/factory queue`. A project that fails is reported, and the others still print. `fleet dashboard` serves one read-only page on this machine, grouped by project. Set `FACTORY_FLEET` to the `fleet.toml` path when you are not inside the fleet directory.

`concurrency` in `fleet.toml`, or `factory fleet queue --concurrency N`, keeps that many `new` and `resume` rows across every project. Later ones print as `hold`. Projects earlier in the file get the slots. `wait`, `skip` and `hold` do not take a slot. Each project still applies its own `[run].concurrency` before the fleet sees the rows. Omit both and there is no fleet-wide cap.

Building stays in the project. Open that project and run `/factory run`, or `/factory run 12` for one issue. The per-project `factory dashboard` still shows that project's pipeline.

Two clones of one GitHub repository both appear if you add both paths. They do not share job files.

## Permissions your tool will ask for

The orchestrator runs `.factory/bin/factory`. The agents it starts run `git`,
`gh` and your project's check commands inside the job's worktree. If your tool
asks before each command, allow these for the repository, or a run stops at
every step waiting for you.

## Using more than one tool

```toml
[tools]
enabled = ["claude", "cursor"]
```

`factory sync` then writes agent files for each. Any one of the tools can run
`/factory run`; they share the same job state in `.factory/jobs/`, so do not
run two orchestrators on the same checkout at the same time.

Cursor also reads `.claude/skills`. With Claude Code and Cursor enabled, the
factory writes the skill only to `.claude/skills/factory`, so Cursor lists it
once. With all three tools enabled it also writes `.agents/skills/factory` for
Codex, and Cursor may list the skill twice; the copies are identical.

## Upgrade

```bash
.factory/bin/factory upgrade                 # the latest release
.factory/bin/factory upgrade --version v0.2.0
```

It replaces `.factory/` (not `.factory/jobs/`), runs `sync`, and leaves
`factory.toml` alone. It refuses a release whose `schema` number differs from
your `factory.toml`; the [changelog](../CHANGELOG.md) says what to change when
that number moves. Commit the result.

## Remove

```bash
rm -rf .factory factory.toml .worktrees
rm -rf .claude/agents/factory-*.md .cursor/agents/factory-*.md .codex/agents/factory-*.toml
rm -rf .claude/skills/factory .agents/skills/factory
git worktree prune
```

Then delete the three `factory` labels on GitHub if you no longer want them.
