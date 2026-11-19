"""Human steps: ask a person, park the job, and read the reply from the CLI or a comment."""

import re

from . import graph
from .util import MARKER, Refused, body, now

COMMAND = re.compile(r"\s*/factory[ \t]+([a-z][a-z-]*)\b[ \t]*(.*)", re.S)
NEEDS_TEXT = {"CHANGES", "FEEDBACK", "REJECT", "ANSWER"}
COMMENT_LIMIT = 60000


class Humans:
    # ---- Asking ------------------------------------------------------------

    def _who(self, job):
        waiting = job["waiting"]
        return f"the author (@{job['author']})" if waiting["who"] == "author" else "a maintainer"

    def _commands(self, job, step, slash=True):
        rows = []
        for outcome in step["on"]:
            text = {"REJECT": " <why>", "ANSWER": " <your answer>", "APPROVE": ""}.get(outcome, " <what to change>")
            rows.append(f"/factory {outcome.lower()} {job['issue']}{text}" if slash
                        else f"`/factory {outcome.lower()}{text}`")
        return rows

    def _ask(self, job, name, step):
        """Post the question where the person will see it, label the issue, and park the job."""
        who = step.get("who", "maintainer")
        labels = self.cfg["labels"]
        label = labels["needs_info"] if who == "author" else labels["needs_human"]
        kind, number = ("pr", job["pr"]) if job["pr"] and not job["merge_sha"] else ("issue", job["issue"])

        title = step.get("ask") or ("this job needs an answer from its author" if who == "author"
                                    else "this job needs a maintainer")
        parts = [f"### Factory: {title}", f"Step `{name}` of job `{job['id']}`."]
        if job.get("reason"):
            parts.append(job["reason"])
        shows = step.get("shows") or job.get("last_file")
        if shows and (self.dir(job) / shows).exists():
            parts.append(f"**{shows}**\n\n{body(self.dir(job) / shows)}")
        how = [f"To reply, comment on this {'pull request' if kind == 'pr' else 'issue'}:"]
        how += [f"- {c}" for c in self._commands(job, step, slash=False)]
        if "ANSWER" in step["on"]:
            how.append("- or answer in plain words")
        if kind == "pr" and name == self.cfg["run"]["escalate_to"]:
            how.append("- or merge this pull request to approve it as it is")
        text = "\n\n".join(parts)[:COMMENT_LIMIT] + "\n\n---\n" + "\n".join(how)

        if step.get("pr_ready") and kind == "pr" and (self.gh.pr(job["pr"], "isDraft") or {}).get("isDraft"):
            self.gh.call("pr", "ready", job["pr"])
        self.gh.comment(kind, number, text)
        self.gh.label(job["issue"], add=label)
        job["waiting"] = {"step": name, "who": who, "label": label, "since": now(), "asked_on": kind}

    def _waiting_line(self, job, name, step):
        return (f"wait {self.tag(job)}: {name} is waiting for {self._who(job)}. "
                f"Reply with: {' | '.join(self._commands(job, step))}")

    # ---- Reading replies ---------------------------------------------------

    def _may_reply(self, job, step, login):
        if step.get("who", "maintainer") == "author":
            return login == job["author"] or login == self.gh.operator()
        return self.gh.can_write(login)

    def _find_reply(self, job, step):
        """The first comment after the factory's last one that answers this step, or None.

        `/factory <outcome> [text]` picks an outcome. On a step with an ANSWER
        outcome, any other comment from an allowed person is the answer.
        """
        threads = [("issue", job["issue"])]
        if job["pr"] and not job["merge_sha"]:
            threads.append(("pr", job["pr"]))
        for kind, number in threads:
            comments = self.gh.comments(kind, number)
            asked = [i for i, c in enumerate(comments) if c["body"].startswith(MARKER)]
            if not asked:
                continue
            for c in comments[asked[-1] + 1:]:
                login = c["author"]["login"]
                if c["id"] in job["consumed"] or not self._may_reply(job, step, login):
                    continue
                m = COMMAND.fullmatch(c["body"])
                if m:
                    outcome = m.group(1).upper()
                    text = re.sub(rf"^#?{job['issue']}\b[ \t]*", "", m.group(2)).strip()
                    if outcome in step["on"] and (text or outcome not in NEEDS_TEXT):
                        return outcome, text, login, c["id"]
                elif "ANSWER" in step["on"]:
                    return "ANSWER", c["body"].strip(), login, c["id"]
        return None

    def _apply_reply(self, job, outcome, text, login, comment_id=None):
        """Record a person's reply to the human step the job is parked in, and move on."""
        name = job["step"]
        step = self.cfg.steps[name]
        d = self.dir(job)
        if comment_id:
            job["consumed"].append(comment_id)
        reason, target = None, None
        if outcome == "REJECT":
            (d / "rejection.md").write_text(f"REJECTED\n\nBy @{login} at the {name} step.\n\n{text}\n")
            reason = f"Rejected by @{login}: {text}"
        elif outcome == "ANSWER":
            job["handoff"].append(f"@{login} answered the questions; their answer is in issue.md.")
        elif text:
            k = len(list(d.glob("feedback-*.md"))) + 1
            (d / f"feedback-{k}.md").write_text(
                f"# Feedback {k}\n\nFrom @{login} at the {name} step ({outcome}).\n\n{text}\n")
            job["handoff"].append(f"@{login} replied {outcome} at the {name} step: read feedback-{k}.md "
                                  "and act on it. It comes before the spec and the reviews where they disagree.")
        if name == self.cfg["run"]["escalate_to"]:
            job["taken"], job["gate_failed"] = {}, False  # a person's feedback gives a fresh budget
        if outcome != "REJECT":
            target = self._ready_target(job, step["on"][outcome]["to"])
        return self._move(job, outcome, reason, to=target)

    def _ready_target(self, job, target):
        """A job with no spec can't go to a step that builds on one: send it back where it came from."""
        target, _ = graph.resolve(self.cfg.steps, target)
        if target in graph.END_STATES or (self.dir(job) / "spec.md").exists():
            return None
        step = self.cfg.steps[target]
        builds_on_spec = step["type"] in ("checks", "review", "merge") or (
            step["type"] == "agent" and self.cfg.role(target) in ("build", "debug", "approve"))
        if not builds_on_spec:
            return None
        origin = job.get("escalated_from")
        if origin in self.cfg.steps and self.cfg.steps[origin]["type"] == "agent":
            return origin
        raise Refused(f"{job['id']} has no spec yet, so it can't go to {target}. Reject it instead.")

    def answer(self, ref, outcome, text):
        """`factory answer`: the person running the factory replies to a human step."""
        job = self.store.open(ref)
        name = job["step"]
        step = self.cfg.steps.get(name)
        if not step or step["type"] != "human":
            raise Refused(f"{job['id']} is at {name}, which isn't waiting for a person")
        outcome = outcome.upper()
        if outcome not in step["on"]:
            raise Refused(f"{name} takes: {', '.join(step['on'])} (not {outcome})")
        text = (text or "").strip()
        if outcome in NEEDS_TEXT and not text:
            raise Refused(f"{outcome} needs words: pass them on stdin")
        login = self.gh.operator()
        if outcome == "ANSWER":
            self.gh.comment("issue", job["issue"], text, marker=False)  # so issue.md carries it
        self.say(self._apply_reply(job, outcome, text, login))

    # ---- A pull request merged by hand -------------------------------------

    def _adopt_merge(self, job):
        """If a person merged the job's PR, continue from the merge step's MERGED edge."""
        if not job["pr"] or job["merge_sha"] or job["step"] in graph.END_STATES:
            return False
        pr = self.gh.pr(job["pr"], "state,mergeCommit")
        if not pr or pr["state"] != "MERGED":
            return False
        old = job["step"]
        self._leave(job, old, self.cfg.steps[old])
        job["merge_sha"] = (pr.get("mergeCommit") or {}).get("oid")
        merges = [n for n, s in self.cfg.steps.items() if s["type"] == "merge"]
        target = self.cfg.steps[merges[0]]["on"]["MERGED"]["to"] if merges else "done"
        entry = {"step": old, "outcome": "MERGED-BY-HAND", "entered_at": job["entered_at"], "at": now(),
                 "reason": f"PR #{job['pr']} was merged on GitHub"}
        job["reason"] = None
        landed, skipped = self._enter(job, target)
        job["history"].append(entry | {"to": landed, "skipped": skipped})
        self.store.save(job)
        self.say(f"{self.tag(job)}: PR #{job['pr']} was merged by hand -> {landed}")
        return True
