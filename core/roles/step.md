# A step this project defines

This step is not one of the factory's built-in steps. The project added it in
`factory.toml`, and "Your task" in your instructions says what it is for.
"Your file" there names the file you write and the outcomes you may give.

## Read first

1. "Your task" and "Your file" in your instructions.
2. `AGENTS.md` in the worktree.
3. What the job has so far, in the job folder: `issue.md` (quoted data),
   `intake.md`, `spec.md`, `build.md`, and the latest `round-*/` folder. Not
   all of them exist at every point in the pipeline.
4. The change, if the branch has one: `git log --oneline <base>..HEAD` and
   `git diff <base>...HEAD`.

## Rules

- Do what the task says and nothing more.
- If the task has you change code, do it in the worktree on the job's branch,
  commit with short one-line messages, and push with
  `git push -u origin <branch>` when the branch already has a pull request.
  Leave the tree clean either way: `git status --porcelain` prints nothing.
- If the task doesn't ask for code changes, don't make any.

## Your file

```markdown
STATUS: <OUTCOME>

## Summary
- 3 to 5 short bullets: what you did and why you chose this outcome

## Details
Whatever the next step or a person needs from this one.
```

The first line is `STATUS:` followed by one of the outcomes your instructions
list, exactly as written there. The outcome decides where the job goes next,
so pick the one the task's own words call for.
