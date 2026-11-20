# Changelog

Versions follow [semantic versioning](https://semver.org). Until 1.0.0, a minor
version may change `factory.toml` keys; the `schema` number in `factory.toml`
changes when it does, and this file says what to change.

## Unreleased

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
