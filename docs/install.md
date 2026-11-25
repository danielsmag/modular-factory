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

This creates `~/.mfactory` (`FACTORY_HOME` when set) and links `~/.local/bin/mfactory`. It also serves the dashboard. The workspace, the list of your projects, is `~/.mfactory/workspace.toml`. Then, in each repository:

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
| `--personal` | Keep `factory.toml`, `.factory/` and the generated files untracked in this checkout. Writes `.git/info/exclude` and does not touch `.gitignore`. |
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
ok    GitHub labels created: factory, factory:needs-info, factory:needs-human
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

### Personal install

Use this when the factory is for you, not for everyone who clones the repository:

```bash
mfactory install --tool claude --personal
```

The same flag works from the installer script and from the Makefile:

```bash
curl -fsSL https://raw.githubusercontent.com/danielsmag/modular-factory/master/install.sh | bash -s -- --tool claude --personal
make install PERSONAL=1 TOOL=claude
```

`.gitignore` stays as it was. The installer appends this block to `.git/info/exclude` (patterns for every tool, so turning one on later does not show new files in `git status`):

```gitignore
# software factory, this checkout only
/factory.toml
/.factory/
/.worktrees/
/.claude/agents/mfactory-*
/.claude/agents/factory-*
/.claude/skills/mfactory/
/.cursor/agents/mfactory-*
/.cursor/agents/factory-*
/.agents/skills/mfactory/
/.codex/agents/mfactory-*
/.codex/agents/factory-*
```

```text
ok    .factory/ installed (version 0.3.1)
ok    factory.toml written
ok    .git/info/exclude gained /factory.toml, /.factory/, /.worktrees/, /.claude/agents/mfactory-*, /.claude/agents/factory-*, /.claude/skills/mfactory/, /.cursor/agents/mfactory-*, /.cursor/agents/factory-*, /.agents/skills/mfactory/, /.codex/agents/mfactory-*, /.codex/agents/factory-*
ok    generated files for claude: 10 written
ok    GitHub labels created: factory, factory:needs-info, factory:needs-human

Next:
  1. In your tool, run `/mfactory setup` to fill in factory.toml (or edit it yourself).
  2. Run `.factory/bin/mfactory doctor`.
  3. Leave factory.toml, .factory/ and the generated files untracked. This checkout ignores them.
  4. Label an issue `factory` and run `/mfactory run`.
```

`git status` does not list `factory.toml`, `.factory/` or `.claude/agents/mfactory-build.md`. `git check-ignore -v factory.toml` names `.git/info/exclude`.

Running `mfactory install` again in that checkout, without `--personal`, does not touch `.gitignore`:

```text
ok    .git/info/exclude already ignores the factory (personal install)
```

`doctor` then reports:

```text
ok    factory.toml and .factory/ are ignored in this checkout (.git/info/exclude). Worktrees built from origin/main do not contain them. A check that needs those files must use the main checkout.
```

Jobs build a worktree from `origin/<base>`. That worktree does not contain `factory.toml` or `.factory/`. A check command that needs those files has to reach the main checkout itself. Skip the commit step below.

To install for the team afterwards, delete that block from `.git/info/exclude`, then run `mfactory install`. That run appends `/.factory/jobs/` and `/.worktrees/` to `.gitignore` and tells you to commit.

`mfactory upgrade` in a personal checkout ends with:

```text
1 upgraded, 0 current
app  run `.factory/bin/mfactory doctor`. Leave factory.toml, .factory/ and the generated files untracked.
```

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
ok    factory.toml is valid: 9 steps on, off: spec-approval, debug, merge, ready-to-verify, verify
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
| `factory.toml and .factory/ aren't on origin/<base> yet` | Commit and push them. Jobs build from that branch. A personal install does not report this: those files are listed in `.git/info/exclude`. |
| `factory.toml and .factory/ are ignored in this checkout` | Expected after `mfactory install --personal`. Worktrees from `origin/<base>` do not contain the factory. |
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
mfactory start dashboard
mfactory stop
mfactory dashboard
```

`start dashboard` serves the page in the background and returns. `stop` ends that process. `dashboard` serves the same page in the foreground until Ctrl C.

The page comes with the global install (`install.sh --global`), so there is one dashboard per machine and no copy in `.factory/`. `.factory/bin/mfactory dashboard`, `start` and `stop` hand off to the global `mfactory`. Without a global install they say to run `install.sh --global`.

## Several projects

The factory in a project only sees that project. Your workspace is `~/.mfactory/workspace.toml`, the list of projects the dashboard and the shared queue cover. `mfactory install` adds the repository it installs into. To add a project that was installed earlier, run this inside it:

```bash
cd /path/to/project-a
mfactory workspace add
mfactory workspace list
mfactory workspace queue
mfactory workspace remove
```

`add` and `remove` take a path when you are not standing in the repository. `remove` also takes the project's folder name. `mfactory dashboard` outside a repository, or in a checkout with no `factory.toml`, uses this same list. `mfactory status` outside a repository does too. `mfactory upgrade` upgrades the global CLI and every project in the workspace, then prints the next step for each one. A project that fails does not stop the others, and each repository still needs its own commit.

A `workspace.toml` in the current directory or a parent still wins, and so does `FACTORY_WORKSPACE`. `mfactory workspace init` writes that local file. `workspace add` accepts a checkout only after the factory is installed there. An older `~/.mfactory/fleet.toml` is renamed to `workspace.toml` the first time it is read.

`workspace queue` runs each project's own `.factory/bin/mfactory queue`. A project that fails is reported, and the others still print. The dashboard has a filter for each project. Choose All for one board, one pipeline per project, one job list, and one config table per project. Choose one project for that project's board, pipeline, jobs, and config. Queue rows that are not jobs yet stay on the board. Queue cards link to the issue and open a drawer with the issue's body and comments, fetched when it opens.

The global dashboard reads job files that each project's own factory wrote. When a project's files come from an older factory, the page says to run `mfactory upgrade` there, and still shows its jobs. When they come from a newer factory than the global one, the page says to run `mfactory upgrade --self` and hides that project's jobs until then.

Each project caps its own jobs with `[run].concurrency` (default 3). The workspace has a second cap, `[workspace].concurrency` in `defaults.toml` (also 3): that many `new` and `resume` rows across every project. `concurrency` in `workspace.toml`, or `mfactory workspace queue --concurrency N`, overrides that cap. Later rows print as `hold`. Projects earlier in the file get the slots. `wait`, `skip` and `hold` do not take a slot. A project's `factory.toml` cannot set the cap across projects.

Building stays in the project. Open that project and run `/mfactory run`, or `/mfactory run 12` for one issue. Inside a project in the workspace, `mfactory dashboard` opens the page with that project selected. A project that is not listed still opens only its own page.

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
mfactory upgrade                              # global CLI and every project in the workspace
mfactory upgrade --dry-run                    # print the plan and change nothing
mfactory upgrade --version v0.2.0
mfactory upgrade --self                       # global CLI only
mfactory upgrade --all                        # workspace projects only
```

`mfactory upgrade` downloads one release and updates the CLI from `install.sh --global`
and every project in the workspace. Standing in an installed checkout that is not listed
updates that checkout too. It prints a plan first, then replaces `.factory/` (not
`.factory/jobs/`), runs `sync`, and leaves `factory.toml` alone. A checkout already on
that version is left as it is, unless its `.factory/` is missing files (for example after an
upgrade from an old CLI). The plan then shows `repair, missing: ...`, and upgrade copies
`.factory/` again and runs `sync`. The copy takes its item list from the release it installs,
not from the CLI that runs it.

The global upgrade and `install.sh --global` keep `workspace.toml`, `dashboard.json`,
`dashboard.log` and `plan-usage.json` in `~/.mfactory` and replace everything else. A
dashboard left running is still on the old code, and the output ends with
`mfactory stop dashboard && mfactory start dashboard` to restart it.

A project whose `schema` differs from the release is skipped. The others still upgrade.
The summary points to that version's [release notes](https://github.com/danielsmag/modular-factory/releases) and tells you to set
`schema`, then run `mfactory upgrade` again.

One project failing does not stop the others. The summary ends with the next step:
commit and push in a team checkout, or leave the files untracked in a personal checkout,
then run doctor.

`mfactory status --all` prints `run mfactory upgrade` when a project in the workspace is behind
the latest release.

## Remove

```bash
rm -rf .factory factory.toml .worktrees
rm -rf .claude/agents/mfactory-*.md .cursor/agents/mfactory-*.md .codex/agents/mfactory-*.toml
rm -rf .claude/skills/mfactory .agents/skills/mfactory
git worktree prune
```

Then delete the three `factory` labels on GitHub if you no longer want them.
