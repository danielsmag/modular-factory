"""The job's card on the tracker's board: a move that answers a human step, and a card moved anywhere else.

A tracker with no board gives no status for any step, and everything here returns at once.
"""

from typing import Any

from .. import board, graph
from ..util import Refused
from .human import COMMAND, NEEDS_TEXT

NOTE_HEADING = "### Factory: the card was moved back"


def is_note(comment: dict[str, Any]) -> bool:
    """A factory comment that explains a card it moved back. It is not a question to a person."""
    return bool(comment.get("factory")) and str(comment.get("body") or "").lstrip().startswith(NOTE_HEADING)


def _same(a: str, b: str) -> bool:
    return a.casefold() == b.casefold()


class Cards:
    def _read_card(self, job: dict[str, Any], name: str, step: dict[str, Any],
                   status: str | None) -> tuple[str, Any]:
        """What the card's place says, without changing anything. `status` is where the card is now.

        `ok`: in place, or nothing to compare. `drift`: where the factory left it, and the mapping now
        says elsewhere. `reply`: a person's move answers the step, with `(outcome, text, login, comment id)`.
        `stray`: a person's move that is no answer, with `(the move, why)`.
        """
        expected: str | None = self.tracker.status_for(name)
        if expected is None or status is None or _same(status, expected):
            return "ok", None
        left: object = (job.get("board") or {}).get("status")
        if not isinstance(left, str) or _same(status, left):
            return "drift", None
        move: dict[str, Any] | None = self.tracker.last_move(job["issue"], status)
        if move is None:
            return "drift", None
        if step["type"] != "human":
            return "stray", (move, f"A person does not answer `{name}`: the factory moves this card while job "
                                   f"`{job['id']}` is at that step. To put the job at another step, run "
                                   f"`mfactory move {job['id']} <step>`.")
        return self._read_drag(job, name, step, move)

    def _read_drag(self, job: dict[str, Any], name: str, step: dict[str, Any],
                   move: dict[str, Any]) -> tuple[str, Any]:
        """A person moved the card of a job that waits for a person. Is that an answer?"""
        to: str = move["to"]
        login: str = move["by"]["login"]
        commands: str = ", ".join(self._commands(job, step, slash=False))
        if step.get("replies") == "tool":
            return "stray", (move, f"`{name}` is answered in the coding tool only: {commands}")
        if not self._may_reply(job, step, login):
            return "stray", (move, f"Only {self._audience(job, step.get('who', 'maintainer'))} "
                                   f"may answer `{name}`.")
        outcomes: list[str] = board.drag_outcomes(self.cfg.steps, self.tracker.status_for, name, to)
        if len(outcomes) > 1:
            return "stray", (move, f"`{to}` could mean {' or '.join(outcomes)}, so answer in a comment "
                                   f"instead: {commands}")
        if not outcomes:
            columns: str = self._drag_choices(name, step)
            how: str = f"Move the card to {columns}, or comment: {commands}" if columns else f"Comment: {commands}"
            return "stray", (move, f"`{to}` is not an answer to `{name}`. {how}")
        outcome: str = outcomes[0]
        text, comment_id = self._drag_words(job, login)
        if not text and outcome in NEEDS_TEXT:
            if outcome == "REJECT":
                text = f"Moved the card to `{to}` on the {self.tracker.label} board."
            elif not (outcome == "FEEDBACK" and name == self.cfg["run"]["escalate_to"]):
                return "stray", (move, f"{outcome} needs words: say them in a comment, then move the card to "
                                       f"`{to}` again. Or comment `/mfactory {outcome.lower()} <your words>`.")
        return "reply", (outcome, text, login, comment_id)

    def _drag_words(self, job: dict[str, Any], login: str) -> tuple[str, Any]:
        """The mover's newest comment since the step was asked, as the words that go with the move."""
        comments: list[dict[str, Any]] = self.tracker.comments(job["issue"])
        asked: list[int] = [i for i, c in enumerate(comments) if c["factory"] and not is_note(c)]
        for comment in reversed(comments[asked[-1] + 1:] if asked else comments):
            mine: bool = comment["author"]["login"] == login and not comment["factory"]
            if mine and comment["id"] not in job["consumed"] and not COMMAND.fullmatch(comment["body"]):
                return comment["body"].strip(), comment["id"]
        return "", None

    def _drag_choices(self, name: str, step: dict[str, Any]) -> str:
        """The statuses a person may move the card to, each with the outcome it means. Empty for none."""
        escalate: str = self.cfg["run"]["escalate_to"]
        shown: list[str] = []
        for status, outcomes in board.drags(self.cfg.steps, self.tracker.status_for, name).items():
            if len(outcomes) != 1 or outcomes[0] == "ANSWER":
                continue  # ambiguous, or a step a plain comment already answers
            outcome: str = outcomes[0]
            wordless: bool = outcome not in NEEDS_TEXT or outcome == "REJECT" or (
                outcome == "FEEDBACK" and name == escalate)
            shown.append(f"`{status}` ({outcome})" if wordless
                         else f"`{status}` ({outcome}, after a comment saying what to change)")
        return ", ".join(shown)

    def _put_back(self, job: dict[str, Any], name: str, move: dict[str, Any], why: str) -> None:
        """Move the card back to its step's status and say why, once. A card that will not move gets no comment."""
        self._place(job, name)
        if (job.get("board") or {}).get("warning"):
            return
        who: str = self.tracker.mention(move["by"]["login"], move["by"].get("name"))
        self.tracker.comment(job["issue"], (
            f"{NOTE_HEADING}\n\n{who} moved this card to `{move['to']}`. Job `{job['id']}` is at step "
            f"`{name}`, so the card is back in `{self.tracker.status_for(name)}`.\n\n{why}"))

    def _settle_card(self, job: dict[str, Any], name: str, kind: str, data: Any) -> None:
        """Act on a card that is out of place and answers nothing: follow the mapping, or move it back."""
        if kind == "drift":
            self._place(job, name)
        elif kind == "stray":
            self._put_back(job, name, *data)
        else:
            return
        self.store.save(job)

    def _settle_cards(self) -> set[str]:
        """Look at the card of every unfinished job, in one call to the tracker.

        Cards out of place are put right. Returns the ids of the jobs a person's move has answered;
        `next` applies those.
        """
        active: list[dict[str, Any]] = [
            j for j in self.store.all()
            if j["step"] not in graph.END_STATES and j["step"] in self.cfg.steps
            and self.tracker.status_for(j["step"]) is not None]
        if not active:
            return set()
        where: dict[Any, str] = self.tracker.statuses([j["issue"] for j in active])
        answered: set[str] = set()
        for j in active:
            name: str = j["step"]
            kind, data = self._read_card(j, name, self.cfg.steps[name], where.get(j["issue"]))
            if kind == "reply":
                answered.add(j["id"])
            elif kind != "ok":
                job: dict[str, Any] | None = self.store.try_open(j["id"])
                if job:
                    self._settle_card(job, name, kind, data)
        return answered

    def sync_cards(self) -> bool:
        """`mfactory tracker sync`: put every unfinished job's card in its step's status. False when one failed.

        For use after the board or `[tracker.jira.columns]` changed. Finished jobs keep their cards.
        """
        jobs: list[dict[str, Any]] = [j for j in self.store.all()
                                      if j["step"] not in graph.END_STATES and j["step"] in self.cfg.steps]
        mapped: list[Any] = [j["issue"] for j in jobs if self.tracker.status_for(j["step"]) is not None]
        where: dict[Any, str] = self.tracker.statuses(mapped) if mapped else {}
        fine: bool = True
        for j in jobs:
            head: str = f"{self.tracker.ref(j['issue'])}  {j['id']}  {j['step']}"
            expected: str | None = self.tracker.status_for(j["step"])
            current: str | None = where.get(j["issue"])
            if expected is None:
                self.say(f"skip   {head}  no status for this step")
            elif current is not None and _same(current, expected):
                self.say(f"ok     {head}  `{current}`")
            else:
                job: dict[str, Any] = self.store.open(j["id"])
                try:
                    self.tracker.move(job["issue"], job["step"])
                except Refused as error:
                    fine = False
                    self.say(f"FAIL   {head}  {error}")
                    continue
                job["board"] = {"status": expected}
                self.store.save(job)
                self.say(f"moved  {head}  `{current or 'unknown'}` -> `{expected}`")
        return fine
