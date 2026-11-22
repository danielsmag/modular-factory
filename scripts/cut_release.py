"""Publish one GitHub release for each merged pull request CI has not released yet.

Master requires a pull request, so this does not push the version commit
straight to master. It opens ``release-vX.Y.Z``, squash-merges it, then tags
that commit and creates the GitHub release. The squash message starts with
``Release vX.Y.Z.`` and names the feature pull request, which is not a
conventional prefix, so the release merge does not cut another release.

Runs only on GitHub Actions, after ``ci`` succeeds on master.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_SCRIPTS: Path = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from release_version import (  # noqa: E402
    Changelog,
    PromotedChangelog,
    ReleaseClassifier,
    ReleaseDecision,
    ReleaseError,
    Version,
    parse_release_commit,
    release_body,
    release_commit_message,
    release_subject,
)

_WORKFLOW: str = ".github/workflows/release.yml"
_BOT_NAME: str = "github-actions[bot]"
_BOT_EMAIL: str = "41898282+github-actions[bot]@users.noreply.github.com"
_GH_TRANSIENT: tuple[str, ...] = ("HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str
    stderr: str


class CommandRunner:
    """Run git and gh in the repository. ``git config`` is never written."""

    def __init__(self, root: Path) -> None:
        self._root: Path = root

    def run(
        self,
        args: list[str],
        *,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        attempts: int = 4 if args and args[0] == "gh" else 1
        result: CommandResult = CommandResult(code=1, stdout="", stderr="")
        for attempt in range(attempts):
            completed: subprocess.CompletedProcess[str] = subprocess.run(
                args,
                cwd=self._root,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            result = CommandResult(
                code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
            if result.code == 0:
                return result
            detail: str = result.stderr.strip() or result.stdout.strip()
            transient: bool = any(token in detail for token in _GH_TRANSIENT)
            if transient and attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
                continue
            if check:
                shown: str = " ".join(args[:4])
                raise ReleaseError(f"{shown} failed ({result.code}): {detail}")
            return result
        return result


@dataclass(frozen=True)
class MergedPullRequest:
    number: int
    title: str
    body: str
    merge_commit_sha: str


@dataclass(frozen=True)
class ListedPullRequest:
    number: int
    state: str
    merge_commit_sha: str


class GitRepository:
    """Commits on ``origin/master`` and the release branch."""

    def __init__(self, root: Path, runner: CommandRunner) -> None:
        self._root: Path = root
        self._runner: CommandRunner = runner

    def fetch_master(self) -> None:
        self._runner.run(["git", "fetch", "origin", "master", "--tags"])

    def master_sha(self) -> str:
        return self._runner.run(["git", "rev-parse", "origin/master"]).stdout.strip()

    def sync_master(self) -> None:
        self.fetch_master()
        self._runner.run(["git", "checkout", "--force", "-B", "master", "origin/master"])

    def message(self, sha: str) -> str:
        return self._runner.run(["git", "log", "-1", "--format=%B", sha]).stdout

    def file_at(self, sha: str, path: str) -> str:
        return self._runner.run(["git", "show", f"{sha}:{path}"]).stdout

    def commits_since_workflow(self) -> list[str]:
        """First-parent commits on master from the one that added the release workflow, inclusive."""
        found: str = self._runner.run(
            [
                "git",
                "log",
                "origin/master",
                "--first-parent",
                "-m",
                "-1",
                "--diff-filter=A",
                "--format=%H",
                "--",
                _WORKFLOW,
            ]
        ).stdout.strip()
        if found == "":
            raise ReleaseError(f"{_WORKFLOW} is not on origin/master")
        added: str = found.splitlines()[0].split("^", 1)[0]
        parent: CommandResult = self._runner.run(["git", "rev-parse", "--verify", f"{added}^"], check=False)
        if parent.code != 0:
            revision: str = "origin/master"
        else:
            revision = f"{parent.stdout.strip()}..origin/master"
        listed: str = self._runner.run(
            ["git", "rev-list", "--first-parent", "--reverse", revision]
        ).stdout.strip()
        if listed == "":
            return []
        return listed.splitlines()

    def fetch_branch(self, branch: str) -> None:
        self._runner.run(["git", "fetch", "origin", branch])

    def remote_branch_sha(self, branch: str) -> str | None:
        listed: str = self._runner.run(["git", "ls-remote", "--heads", "origin", branch]).stdout.strip()
        if listed == "":
            return None
        return listed.split()[0]

    def checkout_branch(self, branch: str, sha: str) -> None:
        self._runner.run(["git", "checkout", "--force", "-B", branch, sha])

    def commit_release(self, version: Version, promoted: PromotedChangelog, pull_request: int) -> None:
        version_path: Path = self._root / "core" / "VERSION"
        changelog_path: Path = self._root / "CHANGELOG.md"
        version_path.write_text(f"{version}\n", encoding="utf-8")
        changelog_path.write_text(promoted.text, encoding="utf-8")
        status: str = self._runner.run(["git", "status", "--porcelain"]).stdout
        for line in status.splitlines():
            path: str = line[3:]
            if path not in {"core/VERSION", "CHANGELOG.md"}:
                raise ReleaseError(f"release commit would touch {path}")
        self._runner.run(["git", "add", "--", "core/VERSION", "CHANGELOG.md"])
        message_path: str = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
                handle.write(release_commit_message(version, pull_request))
                message_path = handle.name
            self._runner.run(
                [
                    "git",
                    "-c",
                    f"user.name={_BOT_NAME}",
                    "-c",
                    f"user.email={_BOT_EMAIL}",
                    "commit",
                    "-F",
                    message_path,
                ]
            )
        finally:
            if message_path:
                os.unlink(message_path)

    def push_branch(self, branch: str) -> None:
        self._runner.run(["git", "push", "--set-upstream", "origin", branch])


class GitHubRepository:
    """Pull requests and releases for one GitHub repository."""

    def __init__(self, repository: str, runner: CommandRunner) -> None:
        self._repository: str = repository
        self._runner: CommandRunner = runner

    def pulls_for_commit(self, sha: str) -> list[MergedPullRequest]:
        result: CommandResult = self._runner.run(
            ["gh", "api", f"repos/{self._repository}/commits/{sha}/pulls"]
        )
        payload: object = json.loads(result.stdout)
        if not isinstance(payload, list):
            raise ReleaseError(f"pulls for {sha} was not a list")
        found: list[MergedPullRequest] = []
        item: object
        for item in payload:
            if not isinstance(item, dict):
                raise ReleaseError(f"pulls for {sha} had a row that was not an object")
            merged_at: object = item.get("merged_at")
            if not isinstance(merged_at, str) or merged_at == "":
                continue
            found.append(self._merged_pull_request(item))
        if found:
            return found
        return self._pull_from_merge_message(sha)

    def pull_request_for_branch(self, branch: str) -> ListedPullRequest | None:
        result: CommandResult = self._runner.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self._repository,
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "20",
                "--json",
                "number,state,mergeCommit",
            ]
        )
        payload: object = json.loads(result.stdout)
        if not isinstance(payload, list):
            raise ReleaseError(f"pull request list for {branch} was not a list")
        chosen: ListedPullRequest | None = None
        item: object
        for item in payload:
            if not isinstance(item, dict):
                raise ReleaseError(f"pull request list for {branch} had a row that was not an object")
            listed: ListedPullRequest = self._listed_pull_request(item)
            if listed.state == "MERGED":
                return listed
            if listed.state == "OPEN":
                chosen = listed
        return chosen

    def create_pull_request(self, branch: str, version: Version, pull_request: int) -> int:
        created: CommandResult = self._runner.run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                self._repository,
                "--base",
                "master",
                "--head",
                branch,
                "--title",
                release_subject(version),
                "--body",
                release_body(pull_request),
            ]
        )
        number_text: str = created.stdout.strip().rstrip("/").split("/")[-1]
        if not number_text.isdigit():
            raise ReleaseError(f"opened {branch} but the pull request url was {created.stdout.strip()!r}")
        return int(number_text)

    def merge_squash(self, number: int, version: Version, pull_request: int) -> str:
        self._runner.run(
            [
                "gh",
                "pr",
                "merge",
                str(number),
                "--repo",
                self._repository,
                "--squash",
                "--delete-branch",
                "--subject",
                release_subject(version),
                "--body",
                release_body(pull_request),
            ]
        )
        for attempt in range(5):
            merged: ListedPullRequest = self.view_pull_request(number)
            if merged.state == "MERGED" and merged.merge_commit_sha != "":
                return merged.merge_commit_sha
            if attempt < 4:
                time.sleep(2)
        raise ReleaseError(f"pull request #{number} merged without a commit sha")

    def view_pull_request(self, number: int) -> ListedPullRequest:
        result: CommandResult = self._runner.run(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                self._repository,
                "--json",
                "number,state,mergeCommit",
            ]
        )
        payload: object = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ReleaseError(f"pull request #{number} was not an object")
        return self._listed_pull_request(payload)

    def ensure_release(self, version: Version, sha: str, notes: str) -> None:
        tag: str = f"v{version}"
        existing: CommandResult = self._runner.run(
            ["gh", "release", "view", tag, "--repo", self._repository],
            check=False,
        )
        if existing.code == 0:
            print(f"{tag} already published")
            return
        notes_path: str = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
                handle.write(notes + "\n")
                notes_path = handle.name
            self._runner.run(
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    "--repo",
                    self._repository,
                    "--target",
                    sha,
                    "--title",
                    str(version),
                    "--notes-file",
                    notes_path,
                ]
            )
        finally:
            if notes_path:
                os.unlink(notes_path)
        print(f"published {tag} at {sha}")

    def _pull_from_merge_message(self, sha: str) -> list[MergedPullRequest]:
        message: str = self._runner.run(["git", "log", "-1", "--format=%s", sha]).stdout.strip()
        marker: str = "Merge pull request #"
        if not message.startswith(marker):
            return []
        rest: str = message[len(marker) :]
        number_text: str = rest.split(" ", 1)[0]
        if not number_text.isdigit():
            return []
        result: CommandResult = self._runner.run(
            [
                "gh",
                "pr",
                "view",
                number_text,
                "--repo",
                self._repository,
                "--json",
                "number,title,body,state,mergeCommit",
            ]
        )
        payload: object = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ReleaseError(f"pull request #{number_text} was not an object")
        if payload.get("state") != "MERGED":
            return []
        return [self._merged_pull_request(payload)]

    def _merged_pull_request(self, row: dict[str, object]) -> MergedPullRequest:
        merge_sha: str = _string(row, "merge_commit_sha")
        if merge_sha == "":
            merge_commit: object = row.get("mergeCommit")
            if isinstance(merge_commit, dict):
                oid: object = merge_commit.get("oid")
                if isinstance(oid, str):
                    merge_sha = oid
        return MergedPullRequest(
            number=_integer(row, "number"),
            title=_string(row, "title"),
            body=_string(row, "body"),
            merge_commit_sha=merge_sha,
        )

    def _listed_pull_request(self, row: dict[str, object]) -> ListedPullRequest:
        merge_sha: str = ""
        merge_commit: object = row.get("mergeCommit")
        if isinstance(merge_commit, dict):
            oid: object = merge_commit.get("oid")
            if isinstance(oid, str):
                merge_sha = oid
        return ListedPullRequest(
            number=_integer(row, "number"),
            state=_string(row, "state"),
            merge_commit_sha=merge_sha,
        )


def _string(row: dict[str, object], key: str) -> str:
    value: object = row.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ReleaseError(f"{key} was not a string")
    return value


def _integer(row: dict[str, object], key: str) -> int:
    value: object = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReleaseError(f"{key} was not an integer")
    return value


class ReleaseCutter:
    """Turn conventional merges on master into tags."""

    def __init__(self, root: Path, repository: str, runner: CommandRunner) -> None:
        self._root: Path = root
        self._git: GitRepository = GitRepository(root, runner)
        self._github: GitHubRepository = GitHubRepository(repository, runner)
        self._classifier: ReleaseClassifier = ReleaseClassifier()

    def cut(self, expected_sha: str) -> None:
        self._git.fetch_master()
        head: str = self._git.master_sha()
        if head != expected_sha:
            print(f"master is {head}, CI ran on {expected_sha}; a newer run will release")
            return
        commits: list[str] = self._git.commits_since_workflow()
        commit_set: set[str] = set(commits)
        released: dict[int, tuple[Version, str]] = {}
        pulls_by_sha: dict[str, list[MergedPullRequest]] = {}
        sha: str
        for sha in commits:
            parsed: tuple[Version, int] | None = parse_release_commit(self._git.message(sha))
            if parsed is not None:
                version: Version
                number: int
                version, number = parsed
                released[number] = (version, sha)
                continue
            pulls_by_sha[sha] = self._github.pulls_for_commit(sha)
        self._publish_existing(released)
        pending: list[tuple[str, MergedPullRequest]] = self._pending(commits, commit_set, pulls_by_sha, released)
        if not pending and not released:
            print("no pull request to release")
            return
        pull: MergedPullRequest
        for _landing, pull in pending:
            self._publish_new(pull)

    def _publish_existing(self, released: dict[int, tuple[Version, str]]) -> None:
        number: int
        version: Version
        sha: str
        for number, (version, sha) in released.items():
            notes: str = Changelog(self._git.file_at(sha, "CHANGELOG.md")).notes_for(version)
            print(f"release commit for #{number} is {sha}")
            self._github.ensure_release(version, sha, notes)

    def _pending(
        self,
        commits: list[str],
        commit_set: set[str],
        pulls_by_sha: dict[str, list[MergedPullRequest]],
        released: dict[int, tuple[Version, str]],
    ) -> list[tuple[str, MergedPullRequest]]:
        grouped: dict[int, list[str]] = {}
        meta: dict[int, MergedPullRequest] = {}
        sha: str
        for sha in commits:
            pull: MergedPullRequest
            for pull in pulls_by_sha.get(sha, []):
                if pull.number in released:
                    continue
                if pull.merge_commit_sha and pull.merge_commit_sha not in commit_set:
                    continue
                grouped.setdefault(pull.number, []).append(sha)
                meta[pull.number] = pull
        landings: list[tuple[str, MergedPullRequest]] = []
        number: int
        shas: list[str]
        for number, shas in grouped.items():
            pull = meta[number]
            landing: str = pull.merge_commit_sha if pull.merge_commit_sha in commit_set else shas[-1]
            landings.append((landing, pull))
        landings.sort(key=lambda item: commits.index(item[0]))
        return landings

    def _publish_new(self, pull: MergedPullRequest) -> None:
        decision: ReleaseDecision | None = self._classifier.classify(pull.title, pull.body)
        if decision is None:
            print(f"skip #{pull.number}: {pull.title!r} has no release prefix")
            return
        self._git.sync_master()
        current: Version = Version.parse((self._root / "core" / "VERSION").read_text(encoding="utf-8"))
        version: Version = current.bumped(decision.bump)
        promoted: PromotedChangelog = Changelog(
            (self._root / "CHANGELOG.md").read_text(encoding="utf-8")
        ).promote(version, pull.title)
        print(f"release #{pull.number}: {pull.title!r} -> v{version} ({decision.bump.value})")
        self._open_and_merge(version, promoted, pull.number)

    def _open_and_merge(self, version: Version, promoted: PromotedChangelog, pull_request: int) -> None:
        branch: str = f"release-v{version}"
        remote_sha: str | None = self._git.remote_branch_sha(branch)
        listed: ListedPullRequest | None = self._github.pull_request_for_branch(branch)
        if listed is not None and listed.state == "MERGED":
            if listed.merge_commit_sha == "":
                raise ReleaseError(f"{branch} merged without a commit sha")
            notes: str = Changelog(self._git.file_at(listed.merge_commit_sha, "CHANGELOG.md")).notes_for(version)
            self._github.ensure_release(version, listed.merge_commit_sha, notes)
            return
        if remote_sha is None:
            self._git.checkout_branch(branch, self._git.master_sha())
            self._git.commit_release(version, promoted, pull_request)
            self._git.push_branch(branch)
        else:
            self._git.fetch_branch(branch)
            parsed: tuple[Version, int] | None = parse_release_commit(self._git.message(remote_sha))
            if parsed is None or parsed[0] != version or parsed[1] != pull_request:
                raise ReleaseError(f"origin/{branch} is not the release for #{pull_request}; delete it and rerun")
            self._git.checkout_branch(branch, remote_sha)
        if listed is None:
            number: int = self._github.create_pull_request(branch, version, pull_request)
        else:
            number = listed.number
        self._git.sync_master()
        merge_sha: str = self._github.merge_squash(number, version, pull_request)
        self._github.ensure_release(version, merge_sha, promoted.notes)


def main() -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print("release: refusing to publish outside GitHub Actions", file=sys.stderr)
        return 2
    expected: str = os.environ.get("RELEASE_SHA", "").strip()
    repository: str = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if expected == "":
        print("release: RELEASE_SHA is not set", file=sys.stderr)
        return 2
    if repository == "":
        print("release: GITHUB_REPOSITORY is not set", file=sys.stderr)
        return 2
    root: Path = Path(__file__).resolve().parents[1]
    try:
        ReleaseCutter(root, repository, CommandRunner(root)).cut(expected)
    except ReleaseError as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
