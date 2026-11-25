"""Choose the next factory version from a merged pull request title.

The release workflow owns ``core/VERSION``. The release notes are the feature
pull request title.
"""

import re
from dataclasses import dataclass
from enum import Enum

_TYPE_NAMES: tuple[str, ...] = (
    "build",
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "hotfix",
    "perf",
    "refactor",
    "revert",
    "style",
    "test",
)
_TITLE: re.Pattern[str] = re.compile(
    r"^(?P<type>" + "|".join(_TYPE_NAMES) + r")"
    r"(?:\([^)\n]+\))?"
    r"(?P<bang>!)?"
    r": "
    r"(?P<subject>\S.*)$"
)
_BREAKING: re.Pattern[str] = re.compile(r"(?m)^BREAKING[ -]CHANGE:")
_VERSION: re.Pattern[str] = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_RELEASE_COMMIT: re.Pattern[str] = re.compile(
    r"^Release v(?P<version>\d+\.\d+\.\d+)\.\n+Pull request: #(?P<number>\d+)\b"
)


class ReleaseError(Exception):
    """The release could not be chosen or published."""


class Bump(Enum):
    """How far the next tag moves."""

    PATCH = "patch"
    MINOR = "minor"
    BREAKING = "breaking"


@dataclass(frozen=True)
class Version:
    """A ``X.Y.Z`` version with no leading ``v``."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> "Version":
        match: re.Match[str] | None = _VERSION.fullmatch(text.strip())
        if match is None:
            raise ReleaseError(f"not a version: {text!r}")
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    def bumped(self, bump: Bump) -> "Version":
        """Next version. A breaking change stays minor while ``major`` is 0."""
        if bump is Bump.BREAKING:
            if self.major == 0:
                return Version(0, self.minor + 1, 0)
            return Version(self.major + 1, 0, 0)
        if bump is Bump.MINOR:
            return Version(self.major, self.minor + 1, 0)
        if bump is Bump.PATCH:
            return Version(self.major, self.minor, self.patch + 1)
        raise ReleaseError(f"unknown bump {bump!r}")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class ReleaseDecision:
    """One pull request that should publish a release."""

    bump: Bump
    breaking: bool
    change_type: str
    subject: str


class ReleaseClassifier:
    """Map a pull request title onto a bump. ``None`` publishes nothing."""

    def classify(self, title: str, body: str) -> ReleaseDecision | None:
        match: re.Match[str] | None = _TITLE.fullmatch(title.strip())
        if match is None:
            return None
        change_type: str = match.group("type")
        subject: str = match.group("subject").strip()
        if subject == "":
            return None
        breaking: bool = match.group("bang") == "!" or _BREAKING.search(body) is not None
        bump: Bump = Bump.BREAKING if breaking else Bump.MINOR if change_type == "feat" else Bump.PATCH
        return ReleaseDecision(
            bump=bump,
            breaking=breaking,
            change_type=change_type,
            subject=subject,
        )


def publish_versions(
    current: Version,
    pending: list[tuple[str, str]],
    missing: list[Version],
) -> list[Version]:
    """Tags this run publishes. Missing releases first, then one bump per conventional pull request."""
    classifier: ReleaseClassifier = ReleaseClassifier()
    versions: list[Version] = list(missing)
    cursor: Version = current
    for title, body in pending:
        decision: ReleaseDecision | None = classifier.classify(title, body)
        if decision is None:
            continue
        cursor = cursor.bumped(decision.bump)
        versions.append(cursor)
    return versions


def release_run_name(versions: list[Version]) -> str:
    """Actions run title, such as ``v0.4.1``."""
    return " ".join(f"v{version}" for version in versions)


def release_subject(version: Version) -> str:
    return f"Release v{version}."


def release_body(pull_request: int) -> str:
    return f"Pull request: #{pull_request}\n\n[skip ci]\n"


def release_commit_message(version: Version, pull_request: int) -> str:
    return f"{release_subject(version)}\n\n{release_body(pull_request)}"


def parse_release_commit(message: str) -> tuple[Version, int] | None:
    """Version and feature pull request recorded on a release commit."""
    match: re.Match[str] | None = _RELEASE_COMMIT.search(message.strip())
    if match is None:
        return None
    return Version.parse(match.group("version")), int(match.group("number"))


def release_notes(title: str) -> str:
    """The GitHub release notes for a feature pull request: its title as one bullet."""
    first_line: str = title.strip().splitlines()[0] if title.strip() else "Release."
    return f"- {first_line}"
