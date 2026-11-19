# Changelog

Versions follow [semantic versioning](https://semver.org). Until 1.0.0, a minor
version may change `factory.toml` keys; the `schema` number in `factory.toml`
changes when it does.

## 0.1.0

First version.

- One `factory.toml` per project: pipeline, checks, models and efforts,
  reviewers, project skills.
- The pipeline is a graph of steps with outcome edges and `max` limits on the
  edges that go back.
- Optional steps, shipped disabled: human approval of the spec, a debugger
  after failing checks, validation after merge or deploy.
- Adapters for Claude Code, Cursor and Codex, written by `factory sync`.
- `factory doctor`, `factory graph`, `factory config show`, and a read-only
  dashboard.

Not yet run inside Claude Code, Cursor or Codex: the pipeline is covered by
automated tests that play the orchestrator and the agents.
