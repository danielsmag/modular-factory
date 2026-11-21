---
name: open-pr
description: >-
  Open a new branch and a pull request into master for modular-factory.
  Use when the user asks to start a branch, open a PR, or ship a change
  to master.
---

# Open a pull request

Base branch is `master`. Never commit on `master`. Never open a pull request against `main`.

## Branch

From the repo root:

```bash
git fetch origin
git checkout -b <topic> origin/master
```

`<topic>` is short kebab-case (`add-pr-skill`). One branch per change.

## Commits

Match `git log --oneline`: one commit per distinct piece, imperative sentence, period at the end.

```
Add a skill for opening a pull request.
```

- Stage explicit paths. Never `git add -A` or `git add .`.
- Pass the message with a HEREDOC.
- No secrets (`.env`, credentials).
- No `--no-verify`, no force push.
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
gh pr create --base master --head <topic> --title "<imperative sentence.>" --body "$(cat <<'EOF'
## Summary
- ...

## Test plan
- [ ] ...

EOF
)"
```

Title uses the same style as commits. Return the pull request URL. Do not merge unless the user asks.
