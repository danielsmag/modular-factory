"""A ticket's priority as one of four levels, read from a GitHub Projects field, Jira, or labels."""

from typing import Any

from ..service.gh import GitHub
from ..util import FactoryError

LEVELS: tuple[str, ...] = ("critical", "high", "normal", "low")
DEFAULT_LABELS: dict[str, str] = {
    "p0": "critical", "priority:critical": "critical", "priority/critical": "critical", "critical": "critical",
    "p1": "high", "priority:high": "high", "priority/high": "high", "high": "high",
    "p3": "low", "priority:low": "low", "priority/low": "low", "low": "low",
}
DEFAULT_JIRA: dict[str, str] = {
    "blocker": "critical", "highest": "critical", "critical": "critical",
    "high": "high",
    "medium": "normal",
    "low": "low", "lowest": "low", "minor": "low", "trivial": "low",
}
# Rows read from a Projects board.
PROJECT_LIMIT = 1000


def _merged(defaults: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    """The defaults with the user's entries over them. Keys match without regard to case."""
    return defaults | {str(key).strip().lower(): value for key, value in overrides.items()}


def jira_level(name: str | None, overrides: dict[str, str]) -> str:
    """The level of a Jira priority name. No priority, or one nobody mapped, is "normal"."""
    if not name:
        return "normal"
    return _merged(DEFAULT_JIRA, overrides).get(name.strip().lower(), "normal")


def label_level(labels: list[str], overrides: dict[str, str]) -> str:
    """The highest level any label maps to; "normal" when none does."""
    table: dict[str, str] = _merged(DEFAULT_LABELS, overrides)
    found: list[str] = [table[label.strip().lower()] for label in labels if label.strip().lower() in table]
    return min(found, key=LEVELS.index) if found else "normal"


def github_project_levels(gh: GitHub, owner: str, number: int, field: str) -> dict[int, str]:
    """Issue number to the raw value of `field` on the Projects board. {} when `gh` fails."""
    try:
        found: object = gh.json("project", "item-list", number, "--owner", owner, "--format", "json",
                                "--limit", PROJECT_LIMIT)
    except (FactoryError, OSError, ValueError):
        return {}
    items: object = found.get("items") if isinstance(found, dict) else None
    wanted: str = field.replace(" ", "").lower()
    out: dict[int, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        content: object = item.get("content")
        ident: object = content.get("number") if isinstance(content, dict) else None
        value: object = next((v for k, v in item.items() if k.replace(" ", "").lower() == wanted), None)
        if isinstance(ident, int) and isinstance(value, str) and value.strip():
            out[ident] = value
    return out


def level_for(ticket_kind: str, raw: dict[str, Any], settings: dict[str, Any],
              project_values: dict[int, str]) -> str:
    """The Projects value through the label map, then Jira priority or labels, then "normal"."""
    maps: dict[str, Any] = settings.get("priority") or {}
    label_map: dict[str, str] = maps.get("labels") or {}
    number: object = raw.get("number")
    if isinstance(number, int) and number in project_values:
        found: str = label_level([project_values[number]], label_map)
        if found != "normal" or project_values[number].strip().lower() in _merged({}, label_map):
            return found
    names: list[str] = [str(label.get("name") if isinstance(label, dict) else label)
                        for label in raw.get("labels") or []]
    if ticket_kind == "jira" and raw.get("priority"):
        return jira_level(str(raw["priority"]), maps.get("jira") or {})
    return label_level(names, label_map)
