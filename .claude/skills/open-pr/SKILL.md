---
name: open-pr
description: >-
  Open a new branch and a pull request into master for modular-factory.
  Use when the user asks to start a branch, open a PR, or ship a change
  to master. Pull request titles use a conventional prefix so a merge
  can publish a release.
---

# Open a pull request

Base branch is `master`. Never commit on `master`. Never open a pull request against `main`.

## Branch

From the repo root:

```bash
git fetch origin
git checkout -b <type>/<topic> origin/master
```

Every branch is named `<type>/<topic>`. `<type>` is the change type the pull request title will use (see the table below): `feat`, `fix`, `hotfix`, `perf`, `revert`, `docs`, `chore`, `ci`, `test`, `style`, `refactor` or `build`. `<topic>` is short kebab-case (`feat/add-pr-skill`, `fix/drawer-scroll`). CI rejects a pull request whose branch does not start with one of those types (or `factory/`). One branch per change.

## Commits

Match `git log --oneline`: one commit per distinct piece, imperative sentence, period at the end.

```
Add a skill for opening a pull request.
```

- Stage explicit paths. Never `git add -A` or `git add .`.
- Pass the message with a HEREDOC.
- No secrets (`.env`, credentials).
- No `--no-verify`, no force push.
- End the commit message and the PR body with the attribution lines the session gives you, if any.
- Amend only when the user asked, the HEAD commit is yours, and it is not pushed.

## Checks

Before the pull request, run the same checks as CI:

```bash
make check
```

That is ruff, `bash -n install.sh`, and pytest. Fix failures before pushing.

## Pull request

Push only when the user asked to open the pull request:

```bash
git push -u origin HEAD
```

```bash
gh pr create --base master --head <type>/<topic> --title "feat: add a health check." --body "$(cat <<'EOF'
## Summary
- ...

## Test plan
- [ ] ...

EOF
)"
```

Return the pull request URL. Do not merge unless the user asks.

If `gh` is unavailable or unauthenticated, say so and stop; do not work around it.

## Pull request title

The title is what gets released. After the pull request merges to `master` and CI passes, GitHub opens `release-vX.Y.Z`, squash-merges it, tags `vX.Y.Z`, and publishes the GitHub release. The release title is that tag. The Actions run that publishes it is named with that tag. That release commit updates `core/VERSION` and moves `## Unreleased` in `CHANGELOG.md`. Do not edit `core/VERSION`. Put user-facing notes under `## Unreleased`.

| Title | Bump |
| --- | --- |
| `feat` | minor (`0.2.1` becomes `0.3.0`) |
| `fix`, `hotfix`, `perf`, `revert`, `docs`, `chore`, `ci`, `test`, `style`, `refactor`, `build` | patch (`0.2.1` becomes `0.2.2`) |
| `type!:` or a `BREAKING CHANGE:` footer | minor while the version is `0.x`; major from `1.0.0` |

A scope is allowed: `feat(dashboard): add an overview.`. A title with no prefix merges and publishes nothing. `Release vX.Y.Z.` is reserved for the release job.

Commits stay imperative sentences. Only the pull request title uses the prefix. With a merge commit, GitHub still reads the pull request title, not the commits on the branch.
