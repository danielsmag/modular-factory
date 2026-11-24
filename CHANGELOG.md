# Changelog

Versions follow [semantic versioning](https://semver.org). Until 1.0.0, a minor
version may change `factory.toml` keys; the `schema` number in `factory.toml`
changes when it does, and this file says what to change.

## Unreleased

- The Agent activity tab reads Cursor's agent transcripts too, so a project that runs only Cursor shows what each agent did. Each run is a list of steps you can open. A closed step shows counts such as "Read 6". An open step shows each file read, search, edit and command. A command's full text opens under its row.
- The dashboard tab shows the factory mark as its icon instead of the browser's default globe.

## 0.13.1

- Job times on the dashboard move while the page is open. A running job's age, and the "just now" and "since" labels, no longer wait for a refresh.

## 0.13.0

- The dashboard draws a project's pipeline from `factory.toml` when that project has no `graph.json`, including when it has no jobs. A `graph.json` already on disk is still the pipeline for that project.

## 0.12.0

- Breaking: the fleet is now the workspace. `~/.mfactory/fleet.toml` is renamed to `workspace.toml` the first time it is read. `mfactory fleet subscribe | unsubscribe | add | remove | list | queue | init` is now `mfactory workspace add | remove | list | queue | init`; `add` and `remove` take a path or default to the repository you are in. `FACTORY_FLEET` is now `FACTORY_WORKSPACE`. `[fleet].concurrency` is now `[workspace].concurrency`; a `[fleet]` table in `factory.toml` names the new key. `mfactory fleet dashboard` and `mfactory dashboard --all` are gone: `mfactory dashboard` and `mfactory start dashboard` always serve every project in the workspace.
- The dashboard page lives only in the global install. `.factory/web/` is no longer copied, and install or upgrade removes a leftover copy. `.factory/bin/mfactory dashboard`, `start` and `stop` hand off to the global `mfactory`, and say to run `install.sh --global` when there is none.
- `mfactory install` adds the repository to the workspace.
- `job.json` and `index.json` carry a `schema` number. The dashboard fills fields an older factory did not write and shows a note to upgrade that project. Files from a newer factory show a note to run `mfactory upgrade --self`, and their jobs are hidden.

## 0.11.0

- `[reviewers.<name>].prompt` and `[steps.<name>].instructions_file` name a file in the repo. The agent reads it after its role prompt. A reviewer you add needs `focus` or `prompt`. A custom step needs `instructions` or `instructions_file`. A missing file, or a path outside the repo, fails sync. Schema stays 1.

## 0.10.0

- `rules` names instruction files for an agent: `AGENTS.md`, `CLAUDE.md`, a rule file, `user:CLAUDE.md`, or `all` for every one found in the repo and on this machine. The default is `[rules] all = ["all"]`, and the builder also reads `AGENTS.md` and `user:CLAUDE.md`. Empty `rules` still reads `AGENTS.md`, or `CLAUDE.md` when `AGENTS.md` is missing. A missing project file fails sync. A missing user file does not; `doctor` warns. Schema stays 1.
- `skills` accepts a project skill, a user skill (`user:name`), a plugin skill (`plugin:skill`, such as `superpowers:test-driven-development`), or `all` for every skill found in the repo and on this machine. A missing user or plugin skill does not fail sync; `doctor` warns. Schema stays 1.

## 0.9.0

- `mfactory upgrade` prints a plan, downloads the release once, and ends with the next step for each project. A project already on that version is left as it is. A schema mismatch is skipped and the others still upgrade. `--dry-run` prints the plan and writes nothing. `mfactory status --all` says to run `mfactory upgrade` when a project is behind.

## 0.8.1

- Dashboard text is larger. Prose uses a proportional font and wraps near 75 characters. Secondary grey is lighter.

## 0.8.0

- `[project] branch_prefix` names job branches. The default `factory/` keeps today's names (`factory/<job-id>`, and `factory/revert-<job-id>` for a revert). `[project] branch_pattern`, when set, must match the branch name when the factory chooses it. A job already on disk keeps the branch stored in `job.json`. Schema stays 1.

## 0.7.0

- The job drawer has an Agent activity tab. It shows what each Claude Code agent did, from its transcripts, with secrets redacted. The prompt names the agent, round, and attempt so a run can be matched to the step.
- A new install writes `factory.toml` from `core/templates/factory.toml`. Each setting has a short explanation and a link to the configuration reference. An existing `factory.toml` is still left as it is.

## 0.6.1

- The GitHub Actions run that publishes a release is named with the version, such as `v0.6.1`.

## 0.6.0

- `ready-to-verify` waits until a person says the deploy is live, then runs verify. It ships off, so a merge still goes straight to verify. `VERIFY: BLOCKED` returns to that step instead of failing the job. The fourth `BLOCKED` in a row goes to `escalated`. When verify is on, `spec.md` needs a `## Verify after deploy` section. Schema stays 1. A project that replaces `[steps.verify].on` must include a `BLOCKED` edge.

## 0.5.0

- `mfactory install --personal` keeps `factory.toml`, `.factory/` and the generated files untracked via `.git/info/exclude`, and leaves `.gitignore` alone. `doctor` accepts that layout. Worktrees built from the base branch do not contain those files.

## 0.4.0

- Checks and verify commands receive `FACTORY_CHECKOUT`, the path of the main checkout. They still run in the worktree.

## 0.3.3

- `mfactory upgrade` upgrades the global CLI, every subscribed project, and the checkout you are in when that checkout is not subscribed. `--self` updates only the global CLI. `--all` upgrades only subscribed projects. One failure does not stop the others.

## 0.3.2

- The GitHub release title is the tag, such as `v0.3.2`.

## 0.3.1

- ci: deploy Pages after the artifact upload finishes.

## 0.3.0

- feat(dashboard): add an overview.

## 0.2.5

- test: drop install tests pinned to 0.2.1.

## 0.2.4

- fix: name the GitHub release with the version.

## 0.2.3

- fix: pass the GitHub token to the release job.

## 0.2.2

- A human step can limit GitHub replies to the operator (`who = "operator"`) or to named logins (`who = ["login-a", "login-b"]`). `replies = "tool"` ignores comments and accepts only `mfactory answer`. `author` and `maintainer` are unchanged. Schema stays 1.
- `[project] pr_title` and `[project] pr_title_pattern` set the pull request title. The builder and intake are told the format. `done` refuses a READY build whose title does not match the pattern. Empty means the spec title and no check. Schema stays 1.
- The dashboard has an Overview tab, between Jobs and Config: running slots, merged jobs with a steady-pace line, cost or time by step, and the job table. Board stays the jobs-by-step view. `mfactory summary` is gone.
- A merge to `master` whose pull request title starts with a conventional prefix (`feat`, `fix`, `hotfix`, and the other usual types) publishes the next `vX.Y.Z` tag and GitHub release after CI passes. The release name is the version, such as `0.2.2`. GitHub opens and squash-merges a pull request that updates `core/VERSION` and this file, then tags that commit.
- The docs are easier to read: larger type, more space, and a little color in headings, code, and the welcome cards. The README links to the docs at the top.

## 0.2.1

- A docs site at <https://danielsmag.github.io/modular-factory/docs/>, in the dashboard's look: sidebar, search, and a page for each guide. The dashboard's DOCS button opens it.

## 0.2.0

The factory no longer merges by default. After approval it marks the pull
request ready and waits at `ready-to-merge`. A hand merge continues from the
merge step's `MERGED` edge. Schema stays 1. A project that never overrode
`approve` or `merge` changes behavior on upgrade. To keep the old behavior,
point `approve` at `merge`, switch `ready-to-merge` off, and set
`[steps.merge] enabled = true`.

- A build that stops before it rewrites `build.md` can leave `progress.md` in the job folder. The next builder of that step is told to read it. `done` deletes the file once the build outcome is accepted.
- `mfactory start dashboard` serves the dashboard in the background. `mfactory stop` ends it. `mfactory dashboard` still stays in the foreground until Ctrl C.
- The command is `mfactory`. Generated agents are `mfactory-*`, and the skill
  command is `/mfactory`. A `/factory` comment on an issue or pull request still
  counts. `core/bin/factory` remains so an upgrade from 0.1.0 can sync, and
  `mfactory sync` removes generated files that still use the old names.
- `install.sh --global` installs `mfactory` for your user. `mfactory install`,
  `upgrade`, `uninstall`, `doctor`, `status` and `sync` manage a project.
  `mfactory upgrade --self` updates that user install.
- The fleet file for a user is `~/.mfactory/fleet.toml`. `install.sh --global`
  installs the command into `~/.mfactory`.
  `mfactory fleet subscribe` adds a checkout. Outside a repository, `mfactory
  dashboard` and `mfactory status` show every subscribed project. `mfactory
  upgrade --all` upgrades each one.
- `factory fleet`: one local queue and one read-only dashboard for several
  installed projects. Each job stays in its own checkout. `[run].concurrency`
  caps one project. `[fleet].concurrency` in `defaults.toml` caps `new` and
  `resume` rows across projects. `fleet.toml` or `--concurrency` overrides
  the fleet cap.
- One dashboard for every subscribed project. Board, pipeline, jobs and config
  are the same page, with a filter for All or one project. `mfactory dashboard`
  inside a subscribed checkout opens that project selected. `fleet.html` is gone.

## 0.1.0

First version. `schema = 1`.

- One `factory.toml` per project: pipeline, checks, models and efforts,
  reviewers, surfaces, project skills. It merges key by key over the defaults,
  and a mistake is reported with the key to fix.
- The pipeline is a graph of steps with outcome edges. A `max` on an edge
  limits how often a step may send a job back; a used-up limit sends the job
  to a maintainer.
- Step types: `agent`, `checks`, `review`, `human`, `merge`, `verify`. A
  project adds its own agent and human steps in `factory.toml`.
- Optional steps, shipped switched off: approval of the spec by a person, a
  debugger after failing checks, validation after merge or deploy.
- Human steps are answered with `/factory <outcome> <issue>` in the coding
  tool or with a comment on the issue or pull request.
- Model and effort per step and per tool, with variants by the job's risk.
- Agents read the project skills listed for them.
- Adapters for Claude Code, Cursor and Codex. `factory sync` writes each
  tool's agent files and the `/factory` skill.
- `install.sh`, `factory init`, `factory upgrade` and `factory doctor`.
- `factory graph`, `factory models`, `factory skills` and `factory config
  show`.
- A read-only dashboard, `factory dashboard`: board, pipeline, jobs and
  configuration.

Known limits:

- No job has yet run inside Claude Code, Cursor or Codex. The pipeline is
  covered by automated tests that play the orchestrator and the agents against
  a stand-in for GitHub. See `docs/design.md`, "What is untested".
- macOS, Linux or WSL only.
