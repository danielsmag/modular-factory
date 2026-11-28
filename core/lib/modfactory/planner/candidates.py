"""Pairs of tickets that might be duplicates, dependencies or overlaps, found without reading the code."""

import itertools
import re
from typing import Any

from ..graph import END_STATES

REFERENCE = re.compile(r"(?<![\w#])(#\d+|[A-Z][A-Z0-9]+-\d+)\b")
WORD = re.compile(r"[a-z]{3,}")
STOPWORDS: frozenset[str] = frozenset({
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "are",
    "was",
    "were",
    "have",
    "has",
    "had",
    "not",
    "but",
    "can",
    "will",
    "would",
    "should",
    "could",
    "into",
    "out",
    "its",
    "their",
    "then",
    "than",
    "when",
    "what",
    "which",
    "who",
    "how",
    "all",
    "any",
    "add",
    "allow",
    "make",
    "use",
    "using",
    "new",
    "via",
    "per",
    "get",
    "set",
})
SIMILAR_MIN = 0.35


def references(text: str) -> set[str]:
    """The tracker refs a text mentions: `#12` and `PROJ-7`."""
    return set(REFERENCE.findall(text))


def _words(ticket: dict[str, Any]) -> set[str]:
    text: str = f"{ticket.get('title') or ''} {ticket.get('excerpt') or ''}".lower()
    return {w for w in WORD.findall(text) if w not in STOPWORDS}


def _reasons(a: dict[str, Any], b: dict[str, Any], words: dict[str, set[str]]) -> tuple[float, list[str]]:
    scored: list[tuple[float, str]] = []
    wa: set[str] = words[a["ref"]]
    wb: set[str] = words[b["ref"]]
    if wa and wb:
        jaccard: float = len(wa & wb) / len(wa | wb)
        if jaccard >= SIMILAR_MIN:
            scored.append((round(jaccard, 3), "similar text"))
    if a.get("project") == b.get("project"):
        for one, other in ((a, b), (b, a)):
            mentioned: set[str] = {f"{one['project']}:{r}" for r in
                                   references(f"{one.get('title') or ''} {one.get('excerpt') or ''}")}
            if other["ref"] in mentioned:
                scored.append((0.9, f"mentions {other['ref']}"))
    shared: list[str] = sorted(set(a.get("files") or []) & set(b.get("files") or []))
    if shared:
        scored.append((min(0.9, round(0.6 + 0.1 * (len(shared) - 1), 3)), f"same files: {', '.join(shared)}"))
    if not scored:
        return 0.0, []
    if set(a.get("labels") or []) & set(b.get("labels") or []):
        scored.append((0.2, "same labels"))
    return max(score for score, _ in scored), [reason for _, reason in scored]


def pairs(tickets: list[dict[str, Any]], max_pairs: int) -> list[dict[str, Any]]:
    """The likeliest pairs, highest score first, at most `max_pairs`. Two finished tickets never pair."""
    words: dict[str, set[str]] = {t["ref"]: _words(t) for t in tickets}
    found: list[dict[str, Any]] = []
    for a, b in itertools.combinations(sorted(tickets, key=lambda t: t["ref"]), 2):
        if a.get("stage") in END_STATES and b.get("stage") in END_STATES:
            continue
        score, reasons = _reasons(a, b, words)
        if reasons:
            found.append({"a": a["ref"], "b": b["ref"], "score": score, "reasons": reasons})
    found.sort(key=lambda p: (-p["score"], p["a"], p["b"]))
    return found[:max(max_pairs, 0)]
