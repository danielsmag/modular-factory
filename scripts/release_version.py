"""Choose the next factory version from a merged pull request title.

The release workflow owns ``core/VERSION`` and the version headings in
``CHANGELOG.md``. A feature pull request only adds notes under ``## Unreleased``.
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
_UNRELEASED: str = "## Unreleased\n"


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


@dataclass(frozen=True)
class PromotedChangelog:
    """``CHANGELOG.md`` after ``## Unreleased`` becomes a version section."""

    text: str
    notes: str


class Changelog:
    """Promote ``## Unreleased`` and read a version section back."""

    def __init__(self, text: str) -> None:
        self._text: str = text if text.endswith("\n") else text + "\n"

    def promote(self, version: Version, fallback_note: str) -> PromotedChangelog:
        start: int = self._text.find(_UNRELEASED)
        if start < 0:
            raise ReleaseError("CHANGELOG.md has no ## Unreleased heading")
        body_start: int = start + len(_UNRELEASED)
        next_heading: int = self._text.find("\n## ", body_start)
        if next_heading < 0:
            raw_body: str = self._text[body_start:]
            tail: str = ""
        else:
            raw_body = self._text[body_start:next_heading]
            tail = self._text[next_heading + 1 :]
        notes: str = raw_body.strip()
        if notes == "":
            first_line: str = fallback_note.strip().splitlines()[0] if fallback_note.strip() else "Release."
            notes = f"- {first_line}"
        heading: str = f"## {version}"
        if f"\n{heading}\n" in self._text or self._text.startswith(f"{heading}\n"):
            raise ReleaseError(f"CHANGELOG.md already has {heading}")
        promoted: str = self._text[:start] + "## Unreleased\n\n" + f"{heading}\n\n" + notes + "\n\n" + tail
        if not promoted.endswith("\n"):
            promoted += "\n"
        return PromotedChangelog(text=promoted, notes=notes)

    def notes_for(self, version: Version) -> str:
        heading: str = f"## {version}\n"
        start: int = self._text.find(heading)
        if start < 0:
            raise ReleaseError(f"CHANGELOG.md has no {heading.strip()} section")
        body_start: int = start + len(heading)
        next_heading: int = self._text.find("\n## ", body_start)
        raw_body: str = self._text[body_start:] if next_heading < 0 else self._text[body_start:next_heading]
        notes: str = raw_body.strip()
        if notes == "":
            raise ReleaseError(f"{heading.strip()} has no notes")
        return notes
