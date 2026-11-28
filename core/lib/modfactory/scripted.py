"""The steps that are scripts: merging, and validation after merge or deploy."""

import os
import time
from pathlib import Path
from typing import Any

from . import checks, graph
from .util import Refused, first_line, run, section, tail

POLL_SECONDS = float(os.environ.get("FACTORY_VERIFY_POLL", "15"))


class Scripted:
    # ---- merge -------------------------------------------------------------

    def _merge(self, job):
        """Sync with the base branch, run the checks again, merge the PR. Returns (outcome, reason)."""
        base = self.cfg.base
        pr = self.gh.pr(job["pr"]) if job["pr"] else None
        if not pr:
            return "FAILED", "The job has no pull request to merge."
        if pr["state"] == "MERGED":
            job["merge_sha"] = (pr.get("mergeCommit") or {}).get("oid")
            return "MERGED", None
        job["last_file"] = None
        self.git(job, "fetch", "origin", base)
        merged = self.git(job, "merge", "--no-edit", f"origin/{base}", check=False)
        if merged.returncode != 0:
            self.git(job, "merge", "--abort", check=False)
            return "FAILED", f"The branch conflicts with {base}:\n\n```\n{merged.stdout.strip()}\n```"
        # Another job may have merged since this one was tested.
        if checks.gate(self.cfg, job, self.round_dir(job), "merge-checks") != "PASS":
            job["last_file"] = f"round-{job['round']}/merge-checks.md"
            return "FAILED", f"The checks failed after syncing with {base}."
        try:
            self.git(job, "push", "origin", job["branch"])
            if pr["isDraft"]:
                self.gh.call("pr", "ready", job["pr"])
            self.gh.call("pr", "merge", job["pr"], f"--{self.cfg['project']['merge_method']}")
        except Refused as e:
            return "FAILED", f"The merge failed:\n\n```\n{e}\n```"
        # GitHub may already have deleted the branch on merge.
        run(["git", "push", "origin", "--delete", job["branch"]], cwd=self.cfg.repo, check=False)
        for _ in range(5):  # GitHub can take a moment to report the merge commit
            job["merge_sha"] = ((self.gh.pr(job["pr"]) or {}).get("mergeCommit") or {}).get("oid")
            if job["merge_sha"]:
                break
            time.sleep(1)
        return "MERGED", None

    # ---- verify ------------------------------------------------------------

    def _verify_path(self, job):
        return str(self.cfg.repo / self.cfg["paths"]["worktrees"] / f"{job['id']}-verify")

    def _verify(self, job, name, step):
        """Run the step's commands. Returns PASS or FAIL, or None when the verifier agent is next."""
        state = job["verify"] or {"attempt": 0, "commands": None, "detail": None}
        job["verify"] = state
        while state["commands"] != "PASS":
            state["attempt"] += 1
            ok, detail = self._verify_commands(job, step)
            if ok:
                state["commands"], state["baseline"] = "PASS", self.signature(job, "verify.md")
            elif state["attempt"] > step["retries"]:
                return self._verify_failed(job, step, detail)
            else:
                self.say(f"{self.tag(job)}: {name} failed (attempt {state['attempt']}), trying again")
            self.store.save(job)
        if step["agent"]:
            return None
        (self.dir(job) / "verify.md").write_text(
            "VERIFY: PASS\n\n## Summary\n"
            f"- The commands passed after {step['after']} on commit {(job['merge_sha'] or '')[:7]}\n"
            "- Details are in verify-checks.md\n")
        job["last_file"] = "verify.md"
        return "PASS"

    def _verify_commands(self, job, step):
        """Wait for the deploy if asked, then run the commands in a checkout of the merge commit."""
        d = self.dir(job)
        if not job["merge_sha"]:
            pr = self.gh.pr(job["pr"]) if job["pr"] else None
            job["merge_sha"] = ((pr or {}).get("mergeCommit") or {}).get("oid")
        sha = job["merge_sha"]
        if not sha:
            return False, "The merge commit of this job's pull request is unknown."
        path = self._verify_path(job)
        self._remove_worktree(path)
        run(["git", "fetch", "origin", self.cfg.base], cwd=self.cfg.repo)
        run(["git", "worktree", "add", "--detach", path, sha], cwd=self.cfg.repo)
        env: dict[str, str] = checks.command_env(self.cfg, job["id"], {
            "FACTORY_MERGE_SHA": sha, "FACTORY_PR": str(job["pr"] or ""), "FACTORY_DEPLOY_URL": step["url"]})
        code, log, problem = 0, "", None
        if step["after"] == "deploy":
            deadline = time.monotonic() + step["timeout_minutes"] * 60
            while True:
                left = max(deadline - time.monotonic(), 1) / 60
                code, out, _ = checks.run_commands([step["wait"]], path, env, left)
                log = out
                if code == 0 or time.monotonic() + POLL_SECONDS >= deadline:
                    break
                time.sleep(POLL_SECONDS)
            if code != 0:
                problem = (f"`{step['wait']}` did not report commit {sha[:7]} live within "
                           f"{step['timeout_minutes']} minutes.")
        if code == 0 and step["commands"]:
            code, out, _ = checks.run_commands(step["commands"], path, env, self.cfg["checks"]["timeout_minutes"])
            log += ("\n" if log else "") + out
            if code != 0:
                problem = f"The commands failed with exit {code}."
        verdict = "PASS" if code == 0 else "FAIL"
        (d / "verify-checks.log").write_text(log)
        (d / "verify-checks.md").write_text(
            f"CHECKS: {verdict}\nCOMMIT: {sha}\nATTEMPT: {job['verify']['attempt']}\n\n## Summary\n"
            f"- Ran after {step['after']}" + (f" against {step['url']}" if step["url"] else "") + "\n"
            f"- {problem or 'Every command passed'}\n\n## Output\nLast {checks.TAIL} lines of verify-checks.log:\n\n"
            f"```\n{tail(log, checks.TAIL)}\n```\n")
        detail = None if code == 0 else f"{problem}\n\n```\n{tail(log, 40)}\n```"
        return code == 0, detail

    def _verify_agent_done(self, job, name, step):
        state = job.get("verify") or {}
        if not step["agent"] or state.get("commands") != "PASS":
            raise Refused(f"{name} isn't waiting for its agent. Run `.factory/bin/mfactory next {job['id']}`.")
        if not self.fresh(job, "verify.md", baseline=state.get("baseline") or [0, 0]):
            raise Refused("verify.md wasn't written by the verifier agent during this step")
        line = first_line(self.dir(job) / "verify.md") or ""
        outcome = line.split()[-1] if line.split() else ""
        if outcome not in ("PASS", "FAIL", "BLOCKED"):
            raise Refused(f"verify.md starts with {line!r}. Its first line must be `VERIFY: PASS`, `VERIFY: FAIL` "
                          "or `VERIFY: BLOCKED`")
        job["last_file"] = "verify.md"
        if outcome == "PASS":
            return self._move(job, "PASS")
        if outcome == "BLOCKED":
            return self._verify_blocked(job, name, step)
        detail = section(self.dir(job) / "verify.md", "Failures") or "The verifier found a failure. See verify.md."
        if state["attempt"] <= step["retries"]:
            state["commands"] = None
            self.store.save(job)
            return f"{self.tag(job)}: {name} failed (attempt {state['attempt']}), trying again"
        return self._move(job, self._verify_failed(job, step, detail), detail)

    def _verify_blocked(self, job: dict[str, Any], name: str, step: dict[str, Any]) -> str:
        """Ask again when someone is waiting. Otherwise the environment miss is a failure."""
        detail: str = (section(self.dir(job) / "verify.md", "Blocked")
                       or "The verifier could not reach the environment. See verify.md.")
        target: str
        target, _skipped = graph.resolve(self._pipe(job).steps, step["on"]["BLOCKED"]["to"])
        if target == name:
            return self._move(job, self._verify_failed(job, step, detail), detail)
        reason: str = ("The verifier could not reach the environment. "
                       "The Blocked section of verify.md says why.")
        return self._move(job, "BLOCKED", reason)

    def _verify_failed(self, job, step, detail):
        """Tell people, in the way `on_failure` asks for. The factory never merges a revert."""
        job["verify"]["detail"] = detail
        d = self.dir(job)
        if not (d / "verify.md").exists() or (first_line(d / "verify.md") or "").endswith("PASS"):
            (d / "verify.md").write_text(f"VERIFY: FAIL\n\n## Summary\n- The check after {step['after']} failed\n\n"
                                         f"## Failures\n{detail}\n")
        job["last_file"] = "verify.md"
        sha = (job["merge_sha"] or "unknown")[:7]
        text = (f"### Factory: verification failed\n\nJob `{job['id']}` merged this pull request as {sha}, "
                f"and the check after {step['after']} failed.\n\n{detail}")
        follow_up = ""
        try:
            if step["on_failure"] == "issue":
                url = self.tracker.of(job["issue"]).create_issue(
                    f"Fix: verification failed after #{job['pr']} ({job['title']})",
                    f"The factory merged #{job['pr']} for {self.tracker.ref(job['issue'])} as {sha}, and the "
                    f"check after {step['after']} failed. Find the cause and fix it.\n\n{detail}",
                    queue=True)
                follow_up = f"\n\nA new issue asks the factory to fix it: {url}"
            elif step["on_failure"] == "revert-pr":
                url = self._revert_pr(job)
                follow_up = f"\n\nA draft pull request reverts the change, for a person to merge: {url}"
        except Refused as e:
            follow_up = f"\n\nThe factory could not open the follow-up ({step['on_failure']}): {e}"
        job["verify"]["follow_up"] = follow_up.strip()
        if job["pr"]:
            self.gh.comment(job["pr"], text + follow_up)
        return "FAIL"

    def _revert_pr(self, job):
        repo, base, sha = self.cfg.repo, self.cfg.base, job["merge_sha"]
        if not sha:
            raise Refused("the merge commit is unknown")
        branch: str = self.cfg.job_branch(job["id"], revert=True, tag=self.tracker.branch_tag(job["issue"]))
        path = Path(self._verify_path(job)).with_name(f"{job['id']}-revert")
        self._remove_worktree(path, branch)
        run(["git", "fetch", "origin", base], cwd=repo)
        run(["git", "worktree", "add", "-b", branch, str(path), f"origin/{base}"], cwd=repo)
        try:
            parents = run(["git", "rev-list", "--parents", "-n", "1", sha], cwd=path).stdout.split()[1:]
            mainline = ["-m", "1"] if len(parents) > 1 else []
            reverted = run(["git", "revert", "--no-edit", *mainline, sha], cwd=path, check=False)
            if reverted.returncode != 0:
                run(["git", "revert", "--abort"], cwd=path, check=False)
                raise Refused(f"`git revert {sha[:7]}` did not apply cleanly")
            run(["git", "push", "-u", "origin", branch], cwd=path)
            return self.gh.call(
                "pr", "create", "--draft", "--base", base, "--head", branch,
                "--title", f"Revert #{job['pr']}: {job['title']}", "--body-file", "-",
                input=f"Reverts #{job['pr']} ({sha[:7]}). The factory's check after merging failed; "
                      f"see the comment on #{job['pr']}. Opened as a draft for a person to decide.").stdout.strip()
        finally:
            self._remove_worktree(path, branch)
