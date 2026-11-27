"""Human steps: ask a person, park the job, and read the reply from the CLI or a comment."""

import re
from collections.abc import Callable
from typing import Any

from .. import config, graph
from ..util import Refused, body, now
from .tracker.base import Tracker

COMMAND = re.compile(r"\s*/(?:m)?factory[ \t]+([a-z][a-z-]*)\b[ \t]*(.*)", re.S)
NEEDS_TEXT = {"CHANGES", "FEEDBACK", "REJECT", "ANSWER"}


def _is_note(comment: dict[str, Any]) -> bool:
    """A factory comment about a card it moved back, which asks nobody anything. See `cards.py`."""
    return str(comment.get("body") or "").lstrip().startswith("### Factory: the card was moved back")


def _at(login: str, name: str | None = None) -> str:
    return f"@{login}"


def audience(who: str | list[str], author: str, operator: str,
             mention: Callable[[str], str] = _at) -> str:
    """Who a human step is waiting for, as a sentence fragment.

    `author` and `operator` are already written as people read them. `mention` writes a listed login.
    """
    if who == "author":
        return f"the author ({author})"
    if who == "operator":
        return f"the operator ({operator})"
    if isinstance(who, list):
        names: list[str] = [mention(login) for login in who]
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} or {names[1]}"
        return ", ".join(names[:-1]) + ", or " + names[-1]
    return "a maintainer"


def ask_title(who: str | list[str], asked: str, mention: Callable[[str], str] = _at) -> str:
    """The comment heading when the step does not set `ask`."""
    if asked:
        return asked
    if who == "author":
        return "this job needs an answer from its author"
    if who == "operator":
        return "this job needs the operator"
    if isinstance(who, list):
        return "this job needs " + audience(who, "", "", mention)
    return "this job needs a maintainer"


class Humans:
    # ---- Asking ------------------------------------------------------------

    def _audience(self, job, who):
        """Who `who` is for this job, with the author and the operator as the job's tracker shows people."""
        tracker: Tracker = self.tracker.of(job["issue"])
        mention = tracker.mention
        operator: str = mention(tracker.operator()) if who == "operator" else ""
        return audience(who, mention(job["author"], job.get("author_name")), operator, mention)

    def _who_of(self, job, step):
        """Who the human step waits for on this job's tracker."""
        return config.who_for(step, self.tracker.of(job["issue"]).kind)

    def _who(self, job):
        return self._audience(job, job["waiting"]["who"])

    def _commands(self, job, step, slash=True):
        rows = []
        for outcome in step["on"]:
            text = {"REJECT": " <why>", "ANSWER": " <your answer>", "APPROVE": "", "VERIFY": ""}.get(
                outcome, " <what to change>")
            rows.append(f"/mfactory {outcome.lower()} {job['issue']}{text}" if slash
                        else f"`/mfactory {outcome.lower()}{text}`")
        return rows

    def _ask(self, job, name, step):
        """Post the question where the person will see it, label the issue, and park the job."""
        tracker: Tracker = self.tracker.of(job["issue"])
        who: str | list[str] = self._who_of(job, step)
        open_pr: bool = bool(job["pr"] and not job["merge_sha"])
        kind, number = ("pr", job["pr"]) if open_pr and tracker.pr_threads else ("issue", job["issue"])
        tool_only: bool = step.get("replies") == "tool"
        operator: str = tracker.operator() if who == "operator" else ""

        title: str = ask_title(who, step.get("ask") or "", tracker.mention)
        parts = [f"### Factory: {title}", f"Step `{name}` of job `{job['id']}`."]
        if who == "operator" or isinstance(who, list):
            parts.append(f"Waiting for {self._audience(job, who)}.")
        if job.get("reason"):
            parts.append(job["reason"])
        shows = step.get("shows") or job.get("last_file")
        if shows and (self.dir(job) / shows).exists():
            parts.append(f"**{shows}**\n\n{body(self.dir(job) / shows)}")
        how: list[str]
        if tool_only:
            how = ["Reply in your coding tool. A comment here does not count:"]
            how += [f"- {c}" for c in self._commands(job, step, slash=True)]
        else:
            how = [f"To reply, comment on this {'pull request' if kind == 'pr' else 'issue'}:"]
            how += [f"- {c}" for c in self._commands(job, step, slash=False)]
            if "ANSWER" in step["on"]:
                how.append("- or answer in plain words")
            columns: str = self._drag_choices(job, name, step)
            if columns:
                how.append(f"- or move this card: {columns}")
        # Asked on the issue of a tracker that is not GitHub, the pull request is named by its address.
        this_pr: str = "this pull request" if kind == "pr" else f"the pull request {job['pr_url']}"
        if open_pr and name == self.cfg["run"]["escalate_to"]:
            how.append(f"- or merge {this_pr} to approve it as it is")
        elif open_pr and step.get("pr_ready"):
            how.append(f"- or merge {this_pr} yourself; the factory will continue from there")
        text = "\n\n".join(parts)[:tracker.comment_limit] + "\n\n---\n" + "\n".join(how)

        if step.get("pr_ready") and open_pr and (self.gh.pr(job["pr"], "isDraft") or {}).get("isDraft"):
            self.gh.call("pr", "ready", job["pr"])
        if kind == "pr":
            self.gh.comment(number, text)
        else:
            self.tracker.comment(number, text)
        label: str | None = self.tracker.waiting_on(job["issue"], who)
        waiting: dict[str, object] = {"step": name, "who": who, "label": label, "since": now(), "asked_on": kind}
        if who == "operator":
            waiting["operator"] = operator
        if tool_only:
            waiting["replies"] = "tool"
        job["waiting"] = waiting

    def _waiting_line(self, job, name, step):
        return (f"wait {self.tag(job)}: {name} is waiting for {self._who(job)}. "
                f"Reply with: {' | '.join(self._commands(job, step))}")

    # ---- Reading replies ---------------------------------------------------

    def _may_reply(self, job, step, login):
        tracker: Tracker = self.tracker.of(job["issue"])
        who: str | list[str] = self._who_of(job, step)
        if who == "author":
            return login == job["author"] or login == tracker.operator()
        if who == "operator":
            return login == tracker.operator()
        if isinstance(who, list):
            return login in who
        return self.tracker.can_write(login, job["issue"])

    def _find_reply(self, job, step):
        """The first comment after the factory's last one that answers this step, or None.

        `/mfactory <outcome> [text]` picks an outcome. A `/factory` comment still counts. On a step with an ANSWER
        outcome, any other comment from an allowed person is the answer. `replies = "tool"` ignores comments.
        """
        if step.get("replies") == "tool":
            return None
        threads = [("issue", job["issue"])]
        if self.tracker.of(job["issue"]).pr_threads and job["pr"] and not job["merge_sha"]:
            threads.append(("pr", job["pr"]))
        for kind, number in threads:
            comments = self.gh.comments(number) if kind == "pr" else self.tracker.comments(number)
            # The question is the factory's last comment that is not a note about a card it moved back.
            asked = [i for i, c in enumerate(comments) if c["factory"] and not _is_note(c)]
            if not asked:
                continue
            for c in comments[asked[-1] + 1:]:
                login = c["author"]["login"]
                if c["factory"] or c["id"] in job["consumed"] or not self._may_reply(job, step, login):
                    continue
                m = COMMAND.fullmatch(c["body"])
                if m:
                    outcome = m.group(1).upper()
                    text = re.sub(rf"(?i)^#?{re.escape(str(job['issue']))}\b[ \t]*", "", m.group(2)).strip()
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
        who: str = self.tracker.of(job["issue"]).mention(login)
        if outcome == "REJECT":
            (d / "rejection.md").write_text(f"REJECTED\n\nBy {who} at the {name} step.\n\n{text}\n")
            reason = f"Rejected by {who}: {text}"
        elif outcome == "ANSWER":
            job["handoff"].append(f"{who} answered the questions; their answer is in issue.md.")
        elif text:
            k = len(list(d.glob("feedback-*.md"))) + 1
            (d / f"feedback-{k}.md").write_text(
                f"# Feedback {k}\n\nFrom {who} at the {name} step ({outcome}).\n\n{text}\n")
            job["handoff"].append(f"{who} replied {outcome} at the {name} step: read feedback-{k}.md "
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
        login = self.tracker.of(job["issue"]).operator()
        if outcome == "ANSWER":
            self.tracker.comment(job["issue"], text, factory=False)  # so issue.md carries it
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
