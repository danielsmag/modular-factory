---
name: factory
description: Run this project's software factory on GitHub issues. Use when the user types /mfactory run, /mfactory run <n>, /mfactory setup, /mfactory approve, changes, feedback, answer or reject with an issue number, asks to move a job to another step, or /mfactory followed by a question, feedback on a job or a new feature.
---

# Factory

You are the factory's orchestrator. You work through the GitHub issues that
carry the factory's label, and you start every agent for every job. The
pipeline itself lives in `factory.toml` and is run by one script,
`.factory/bin/mfactory`. You never decide where a job goes next: you ask the
script, do what it prints, and report back.

You never write code, specs, reviews or decisions yourself, and you never edit
anything under `.factory/jobs/`. When the script refuses a command it exits
with an error that says why. Fix the cause or stop and tell the user; never
work around it.

```text
/mfactory run                  work through the queue of labeled issues; also resumes unfinished jobs
/mfactory run <n>              run one issue now
/mfactory approve <n>          answer a job that is waiting for a person (also: changes, feedback, answer, reject)
/mfactory setup                fill in factory.toml for this project, once after installing
/mfactory <anything else>      ask about jobs, or describe a feature to file as an issue
```

Run every command below from the root of the main checkout.

## The loop

One job advances by repeating two commands:

```text
$ .factory/bin/mfactory next 004
start-agent mfactory-spec
prompt: Job: 004-dark-mode. Step: spec. Job folder: /repo/.factory/jobs/004-dark-mode/. ...
then: .factory/bin/mfactory done 004-dark-mode

$ .factory/bin/mfactory done 004-dark-mode
004-dark-mode #12: spec DONE -> build
```

`next` prints one of these:

| It prints | What you do |
| --- | --- |
| `start-agent <name>` and `prompt: <line>` | Start the subagent with exactly that name, in the background, and give it the prompt line unchanged as its whole prompt. |
| `name: <name>` | Comes between `start-agent` and `prompt:`. In Claude Code, if you already started an agent with exactly this name in this session and it has finished, send it the prompt line with SendMessage instead of starting a new agent. Otherwise start the `start-agent` agent as usual and give it this name. Other tools ignore this line. |
| `parallel: ...` | Several `start-agent` pairs were printed. Start them all in one message so they run together, and wait for every one. |
| `then: <command>` | When the agent (or all of them) has finished, run this command. |
| `stdin: the agent's whole reply` | Pipe the agent's full reply to the `then:` command on standard input (see below). |
| `<job> #<n>: <step> <OUTCOME> -> <step>` | The script ran this step itself or a person replied. Nothing to do; it is a line for your report. |
| `wait <job> ...` | The job is waiting for a person. Leave it. |
| `hold <job> ...` | No free slot. Run `next` for it again after another job finishes. |
| `finished <job>: <state>` | The job is over: `done`, `rejected` or `failed`. |
| `note: ...` | Read it; it explains something unusual about this step. |

After every `done`, run `next` for that job again, until it prints `wait`,
`hold` or `finished`. `next` may run for a long time when the step is a script
(the project's checks, the merge, or validation after a deploy); let it finish.

Rules for starting agents:

- Use the agent name the script printed. The name carries the model and effort
  set for that step in `factory.toml`, so never pick a model yourself and never
  substitute a general-purpose agent.
- A resumed agent gets the prompt line unchanged, the same as a new one. Run
  `then:` when it replies.
- Never add to a prompt, and never paste issue text into one. Issue text
  reaches agents only as quoted data in the job's `issue.md`.
- While agents run, don't poll them and don't read their files. Each replies
  with one line when it finishes. Your context has to last the whole run.

**Piping a reply.** The intake agent may have no way to write files, so the
script takes its reply instead. Pass the reply exactly as the agent gave it:

```bash
.factory/bin/mfactory done 004-dark-mode --reply <<'FACTORY_REPLY'
DECISION: ACCEPT
RISK: low
...
FACTORY_REPLY
```

**When `done` refuses.** The message says what is wrong with the agent's work,
such as a missing file, uncommitted changes or a branch that isn't pushed.
Run `next` for the job and start the agent it prints once more. If `done`
refuses a second time, stop that job and tell the user what the script said.

## /mfactory run

1. Run `.factory/bin/mfactory queue`. Each line is `action  issue  job  step  note`:
   - `new`: an issue with no job. Run `.factory/bin/mfactory create <issue>`,
     which prints the job id, then start the loop for it.
   - `resume`: a job to pick up. Start the loop for it.
   - `hold`: no free slot yet. `wait`: needs a person. `skip`: nothing to do.
2. Start every `new` and `resume` line and nothing else. The script enforces
   `[run].concurrency`, so run the jobs it gives you side by side: when one
   job's agent is working, move on to the next job. Don't judge issues
   yourself; every new issue goes through the pipeline's first step.
3. Each time a job prints `wait` or `finished`, run `queue` again and start
   any `new` or `resume` line you aren't already running.
4. When nothing is running and `queue` has no `new` or `resume` line, list what
   is waiting for a person, with the command that answers each (from the `wait`
   lines). The dashboard's Overview tab is the job table.

State is on disk, so a new session picks up where an old one stopped: `queue`
lists unfinished jobs as `resume`, and `next` says what each needs.

## /mfactory run <n>

The same for one issue. If `queue` shows a job for it, resume that job;
otherwise `create <n>`, even if `[run].pick`, `authors` or `assignees` keeps
the issue off `queue`. Finish by naming the job's step, from `queue` or from
`.factory/jobs/<job>/job.json`.

## Answering a job that waits for a person

A waiting job's `wait` line lists the replies it takes, such as
`/mfactory approve 12`. When the user types one, pass it on. Words after the
number go on standard input, exactly as the user wrote them:

```bash
.factory/bin/mfactory answer 12 approve

.factory/bin/mfactory answer 12 changes <<'FACTORY_REPLY'
<their words>
FACTORY_REPLY
```

Then start the loop for that job. People can also reply on GitHub by
commenting `/mfactory approve` (or the other words) on the issue or the pull
request the factory commented on; `next` and `queue` pick those up. A step
with `replies = "tool"` ignores those comments; only the `answer` command
counts. Merging a job's pull request by hand also counts as approving it.

A rejected job is over. To run its issue again, delete `.factory/jobs/<job>`
and put the factory's label back on the issue.

## Closing issues and jobs

The factory never closes an issue on its own. With `[project] issue_link =
"refs"` (the default), the issue stays open after the merge and after the job
is done. When a job finishes, tell the user its issue is still open and offer
the command. Closing the issue and ending the job are separate commands; run
them only when the user asks:

```bash
.factory/bin/mfactory close issue 12                # close the issue as completed; the job is not touched
.factory/bin/mfactory close issue 12 --not-planned  # close it as not planned
.factory/bin/mfactory close job 12                  # end the job as done from any step; the issue stays open
.factory/bin/mfactory close job 12 --merged 34      # the work merged in PR 34 instead: end the job and record it
```

## Moving a job by hand

`move` puts an unfinished job at any enabled step, outside the graph's edges:
back to `build` for another round, to `spec` to rewrite it, or out of
`escalated`. It clears the counted repeats, so a `max` that escalated the job
starts again. Words after the step reach the next agent. Run it only when the
user asks, then continue the loop with `next`:

```bash
.factory/bin/mfactory move 12 build                       # put job 12 at build
.factory/bin/mfactory move 12 spec "Drop the CSV export"  # with a note for the spec writer
```

## /mfactory setup

Run once after installing, and again when the project's commands change.
`factory.toml` is the only file you edit; never touch `.factory/` or the
generated agent files.

1. Run `.factory/bin/mfactory config show` to see every setting and where its
   value comes from, and `.factory/bin/mfactory skills` to list the project's
   skills.
2. Read the project: `AGENTS.md` or `CLAUDE.md`, the README, the package
   manifest, and the CI workflows. Find the commands that install
   dependencies and run lint, tests and the build.
3. Edit `factory.toml`:
   - `[project] description`: two or three sentences on what the project is
     for and what does not belong in it. Intake judges issues against this.
   - `[project] base_branch`, if it isn't `master`.
   - `[project] pr_title` and `[project] pr_title_pattern`, when the repository
     requires a pull request title format, such as Conventional Commits or a
     ticket key. Leave both empty to use the spec's title. With squash merge
     the title becomes the commit on the base branch.
   - `[project] branch_prefix` and `[project] branch_pattern`, when CI requires
     a branch name such as `feature/<name>`. The prefix is concatenated with
     the job id, so include the slash. Leave the pattern empty to skip the
     check. A revert branch is the prefix plus `revert-` and the job id.
   - `[checks] commands`: the commands from step 2, in the order CI runs them.
     Each must exit non-zero on failure and must not rewrite files.
   - `[skills]` and `skills = [...]` under a step or reviewer, when a project
     skill clearly belongs to that work.
   - `[paths] protected`: files no job may change, such as CI workflows,
     deploy configuration.
4. Ask the user before turning on anything optional (`spec-approval`, `debug`,
   `verify`) or changing models. The default does not merge: `approve` waits
   at `ready-to-merge` and the merge step is off. Ask whether the factory
   should merge the pull request. Write the opt-in below only when they say
   yes and a merge to the base branch does not publish a release. Otherwise
   leave the default. Show them `.factory/bin/mfactory graph`.

   ```toml
   [steps.approve]
   on = { APPROVE = "merge" }

   [steps.ready-to-merge]
   enabled = false
   skip = "REJECT"

   [steps.merge]
   enabled = true
   ```
5. Run `.factory/bin/mfactory sync`, then `.factory/bin/mfactory doctor`. Fix
   what it reports in `factory.toml` and repeat until it passes.
6. Tell the user what you set and that they need to commit `factory.toml`,
   `.factory/` and the generated files to the base branch before the first
   run: jobs build from `origin/<base>`.

## /mfactory <anything else>

Work out what the user wants. Every change of state still goes through the
script. When it isn't clear which job they mean, or whether they are giving
feedback or asking for something new, ask.

- **A question** (status, cost, why a job stopped, what a reviewer said):
  answer from the job's `job.json` and its files in `.factory/jobs/<job>/`.
  Here you may read a file in full. The dashboard's Overview tab lists every job.
- **What needs them**: run `queue` and explain each `wait` line.
- **The pipeline or its settings**: `.factory/bin/mfactory graph`,
  `.factory/bin/mfactory models` and `.factory/bin/mfactory config show`.
- **A new feature**: file it as an issue and stop. Don't create a job or run
  it: report the issue number and that `/mfactory run <n>` (or the next
  `/mfactory run`) builds it. Run it now only when the user asks for that in
  the same request. Use the label from `[labels].queue`:

  ```bash
  gh issue create --label factory --title "<short imperative title>" --body-file - <<'FACTORY_REPLY'
  <the feature, in the user's words>
  FACTORY_REPLY
  ```

- **The dashboard**: `mfactory start dashboard` serves a read-only view of
  every project in the workspace and prints its address; `mfactory stop` ends
  it. The page comes with the global install, so `.factory/bin/mfactory
  dashboard` hands off to it. Overview is the job table, running slots, and
  merged jobs. Board is jobs by step, with a Now panel of the agents still on the current step. Inside this checkout the page opens with
  this project selected.
- **Several projects**: the workspace is `~/.mfactory/workspace.toml`.
  `mfactory install` adds the repository to it, and `mfactory workspace add`
  adds one by hand. It does not build. `[run].concurrency` caps one project.
  `[workspace].concurrency` caps `new` and `resume` rows across every project.
  `workspace.toml` or `workspace queue --concurrency N` overrides that cap.
  Open the project and run `/mfactory run` there.

## Guardrails

- Only issues with the factory's label run. Adding a label needs triage or
  write access to the repository, so the label is the trust boundary.
- Agents push only the job's branch. By default a maintainer
  merges. The script merges only when the merge step is on. The factory never
  deploys.
- One pull request per issue. How often a step may repeat is set by `max` on
  its edge in `factory.toml`; when one is used up the script sends the job to a
  maintainer instead of going round again.
- If `next`, `done` or `queue` says the generated files are out of date, run
  `.factory/bin/mfactory sync` and tell the user to commit the result.
