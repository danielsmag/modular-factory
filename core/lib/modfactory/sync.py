"""`mfactory sync`: write each enabled tool's agent files and the orchestrator skill from factory.toml.

The prompts live once, in `.factory/roles/`. A generated agent file holds only
what differs per tool (name, model, effort, permissions) and what comes from
the project's config (skills, context, custom instructions).
"""

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, LiteralString

from . import graph
from .config import BUILTIN_REVIEWERS, Config
from .util import CORE, GENERATED, ConfigError, is_generated

SKILL_DIRS: list[str] = [".agents/skills", ".claude/skills", ".cursor/skills", ".codex/skills"]
USER_SKILL_DIRS: list[str] = [".agents/skills", ".claude/skills", ".cursor/skills", ".codex/skills"]
PLUGIN_DIRS: dict[str, str] = {"claude": ".claude/plugins", "cursor": ".cursor/plugins", "codex": ".codex/plugins"}
OWN_SKILLS: set[str] = {"factory", "mfactory"}
NOTE: LiteralString = f"{GENERATED}: change factory.toml, then run `.factory/bin/mfactory sync`."

DESCRIPTIONS: dict[str, str] = {
    "intake": "Decides whether an issue is worth building and at what scope, and writes intake.md.",
    "spec": "Turns an accepted issue into spec.md with testable acceptance criteria.",
    "build": "Implements the spec on the job's branch, opens the pull request, and writes build.md.",
    "debug": "Finds why the project's checks fail and writes a diagnosis. Never changes code.",
    "post-debug": ("Investigates a problem found after the job's pull request merged and writes a diagnosis "
                   "for a follow-up fix. Never changes code."),
    "approve": "Decides whether the job's pull request merges or goes to a person.",
    "verify": "Checks the merged change against the spec's acceptance criteria.",
    "custom": "A step this project defines in factory.toml.",
}
CLAUDE_TOOLS: dict[str, str] = {
    "intake": "Read, Grep, Glob, Write",
    "spec": "Read, Grep, Glob, Bash, Write, Skill",
    "build": "Read, Grep, Glob, Bash, Edit, Write, Skill",
    "custom": "Read, Grep, Glob, Bash, Edit, Write, Skill",
}
CLAUDE_TOOLS_DEFAULT = "Read, Grep, Glob, Bash, Write, Skill"


# ---- Project, user and plugin skills ---------------------------------------


def skill_home() -> Path:
    """Where user skills and installed plugins live. Tests replace this."""
    return Path.home()


def resolve_skill(repo: Path, name: str) -> Path:
    for folder in SKILL_DIRS:
        path: Path = Path(repo) / folder / name / "SKILL.md"
        if path.is_file():
            return path.relative_to(repo)
    raise ConfigError(f'skill "{name}" not found in {", ".join(SKILL_DIRS)}')


def _skill_description(path: Path) -> str:
    match = re.search(r"^description:\s*(.+)$", path.read_text(errors="replace"), re.M)
    if not match:
        return ""
    return match.group(1).strip().strip("\"'")


def _skill_segment(name: str) -> bool:
    if not name or name in {".", ".."}:
        return False
    return not any(part in name for part in ("/", "\\", "\n", "\r", "\t"))


def classify_skill(raw: str) -> tuple[str, str]:
    """`(kind, name)` for one `skills` entry. `kind` is `all`, `project`, `user` or `plugin`."""
    if raw == "all":
        return "all", "all"
    if raw.startswith("user:"):
        name = raw.removeprefix("user:")
        if not _skill_segment(name):
            raise ConfigError(
                f'skill "{raw}" needs a user skill name after "user:", such as user:python-best-practices')
        return "user", name
    if ":" in raw:
        plugin, skill = raw.split(":", 1)
        if not _skill_segment(plugin) or not _skill_segment(skill):
            raise ConfigError(f'skill "{raw}" needs a plugin skill id, such as superpowers:test-driven-development')
        return "plugin", raw
    if not _skill_segment(raw):
        raise ConfigError(f'skill "{raw}" is not a skill name')
    return "project", raw


def _tilde(path: Path, home: Path) -> str:
    try:
        relative = path.resolve().relative_to(home.resolve())
    except ValueError:
        return path.as_posix()
    return "~/" + relative.as_posix()


def found_skills(repo: Path) -> list[tuple[str, str, str]]:
    """Every project skill on disk, as (name, path, description), without the factory's own."""
    seen: set[str] = set()
    rows: list[tuple[str, str, str]] = []
    for folder in SKILL_DIRS:
        for path in sorted((Path(repo) / folder).glob("*/SKILL.md")):
            name = path.parent.name
            if name in OWN_SKILLS or name in seen:
                continue
            seen.add(name)
            rows.append((name, str(path.relative_to(repo)), _skill_description(path)))
    return rows


def _user_skill_path(home: Path, name: str) -> Path | None:
    for folder in USER_SKILL_DIRS:
        path = home / folder / name / "SKILL.md"
        if path.is_file():
            return path
    return None


def found_user_skills(home: Path) -> list[tuple[str, str, str]]:
    """User skills on this machine, as (name, `~/` path, description). The first folder wins."""
    seen: set[str] = set()
    rows: list[tuple[str, str, str]] = []
    for folder in USER_SKILL_DIRS:
        root = home / folder
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            name = path.parent.name
            if name.startswith(".") or name in seen:
                continue
            seen.add(name)
            rows.append((name, _tilde(path, home), _skill_description(path)))
    return rows


def _codex_enabled_plugins(home: Path) -> set[str]:
    """Plugin names Codex has enabled. A missing or unreadable config enables none."""
    path = home / ".codex" / "config.toml"
    if not path.is_file():
        return set()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return set()
    plugins = data.get("plugins", {})
    names: set[str] = set()
    if not isinstance(plugins, dict):
        return names
    for key, value in plugins.items():
        plugin = str(key).split("@", 1)[0]
        if isinstance(value, dict) and value.get("enabled") is False:
            continue
        if plugin:
            names.add(plugin)
    return names


def _claude_installs(home: Path) -> list[tuple[str, Path]]:
    """`(plugin, install path)` from Claude's `installed_plugins.json`, when that file exists."""
    record = home / ".claude" / "plugins" / "installed_plugins.json"
    if not record.is_file():
        return []
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    plugins = data.get("plugins", {}) if isinstance(data, dict) else {}
    if not isinstance(plugins, dict):
        return []
    rows: list[tuple[str, Path]] = []
    for key, entry in plugins.items():
        plugin = str(key).split("@", 1)[0]
        if not plugin:
            continue
        entries = entry if isinstance(entry, list) else [entry]
        for item in entries:
            raw: object = None
            if isinstance(item, dict):
                raw = item.get("installPath") or item.get("install_path")
            elif isinstance(item, str):
                raw = item
            if isinstance(raw, str) and raw:
                rows.append((plugin, Path(raw)))
    return rows


def _cache_plugin_skills(cache: Path) -> list[tuple[str, str, Path]]:
    """`(plugin, skill, SKILL.md)` under a tool's plugin cache."""
    if not cache.is_dir():
        return []
    rows: list[tuple[str, str, Path]] = []
    seen: set[Path] = set()
    for pattern in ("*/*/*/skills/*/SKILL.md", "*/*/skills/*/SKILL.md"):
        for path in cache.glob(pattern):
            if path in seen or not path.is_file() or path.parent.parent.name != "skills":
                continue
            seen.add(path)
            holder = path.parent.parent.parent
            plugin = holder.name if holder.parent.parent == cache else holder.parent.name
            rows.append((plugin, path.parent.name, path))
    return rows


def found_plugin_skills(home: Path, tool: str) -> list[tuple[str, Path]]:
    """`(plugin:skill, SKILL.md)` this tool can see. Claude's install record wins over the cache."""
    enabled: set[str] | None = _codex_enabled_plugins(home) if tool == "codex" else None
    chosen: dict[str, Path] = {}
    cache = home / PLUGIN_DIRS[tool] / "cache"
    for plugin, skill, path in _cache_plugin_skills(cache):
        if enabled is not None and plugin not in enabled:
            continue
        listed = f"{plugin}:{skill}"
        previous = chosen.get(listed)
        if previous is None or path.stat().st_mtime >= previous.stat().st_mtime:
            chosen[listed] = path
    if tool == "claude":
        for plugin, root in _claude_installs(home):
            if not root.is_dir():
                continue
            for path in sorted(root.glob("skills/*/SKILL.md")):
                if path.is_file():
                    chosen[f"{plugin}:{path.parent.name}"] = path
    return sorted(chosen.items())


class SkillIndex:
    """Plugin skill files for one home directory, looked up once per tool."""

    def __init__(self, home: Path) -> None:
        self.home: Path = home
        self._plugins: dict[str, dict[str, Path]] = {}

    def plugin_path(self, tool: str, listed: str) -> Path | None:
        if tool not in self._plugins:
            self._plugins[tool] = dict(found_plugin_skills(self.home, tool))
        return self._plugins[tool].get(listed)

    def plugin_display(self, tool: str, listed: str) -> str | None:
        path = self.plugin_path(tool, listed)
        if path is None:
            return None
        return _tilde(path, self.home)


# ---- Agents, independent of the tool ---------------------------------------


@dataclass(frozen=True)
class SkillRef:
    listed: str                 # the string to put in `skills`: `testing`, `user:name`, `plugin:skill`
    kind: str                   # `project`, `user` or `plugin`
    name: str
    path: str | None            # repo-relative, or `~/...` for a user skill; plugins resolve per tool
    via_all: bool               # expanded from `all`, so a tool only sees plugins it has installed


@dataclass(frozen=True)
class RuleRef:
    listed: str                 # `AGENTS.md`, `.claude/rules/foo.md`, or `user:CLAUDE.md`
    kind: str                   # `project` or `user`
    path: str | None            # repo-relative, or `~/...`; None when a user file is not installed
    via_all: bool


_PROJECT_RULE_HINT = (
    "AGENTS.md, CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md, "
    "or a file under .claude/rules, .cursor/rules, or .agents/rules"
)
_USER_RULE_HINT = (
    "user:CLAUDE.md, user:.codex/AGENTS.md, user:.agents/AGENTS.md, "
    "or a file under user:.claude/rules or user:.cursor/rules"
)


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _rule_parts_ok(relative: str) -> bool:
    parts = relative.split("/")
    return bool(parts) and not any(part in {"", ".", ".."} for part in parts)


def _under_rules(relative: str, prefix: str, suffixes: tuple[str, ...]) -> bool:
    if not relative.startswith(prefix):
        return False
    rest = relative[len(prefix):]
    return _rule_parts_ok(rest) and rest.endswith(suffixes)


def _project_rule_ok(relative: str) -> bool:
    if relative in {"AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"}:
        return True
    return (_under_rules(relative, ".claude/rules/", (".md",))
            or _under_rules(relative, ".cursor/rules/", (".md", ".mdc"))
            or _under_rules(relative, ".agents/rules/", (".md",)))


def _user_rule_relative(name: str) -> str:
    """Home-relative path for a `user:` rule, or a config error when the path is not allowed."""
    if name == "CLAUDE.md" or name == ".claude/CLAUDE.md":
        return ".claude/CLAUDE.md"
    allowed = name in {".codex/AGENTS.md", ".agents/AGENTS.md"}
    allowed = allowed or _under_rules(name, ".claude/rules/", (".md",))
    allowed = allowed or _under_rules(name, ".cursor/rules/", (".md", ".mdc"))
    if not allowed:
        raise ConfigError(f'rule "user:{name}" must be {_USER_RULE_HINT}')
    return name


def _user_rule_listed(relative: str) -> str:
    if relative == ".claude/CLAUDE.md":
        return "user:CLAUDE.md"
    return f"user:{relative}"


def _rule_relatives(root: Path, prefix: str, patterns: tuple[str, ...]) -> list[str]:
    base = root / prefix
    if not base.is_dir():
        return []
    found: set[str] = set()
    for pattern in patterns:
        for path in base.glob(pattern):
            if not path.is_file() or path.name == "SKILL.md" or not _contained(path, root):
                continue
            relative = path.relative_to(root).as_posix()
            if _rule_parts_ok(relative):
                found.add(relative)
    return sorted(found)


def found_project_rules(repo: Path) -> list[tuple[str, str]]:
    """`(listed, repo-relative path)` for instruction files in the repository."""
    rows: list[tuple[str, str]] = []
    for name in ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md"):
        if (repo / name).is_file():
            rows.append((name, name))
    claude = repo / ".claude" / "CLAUDE.md"
    if claude.is_file() and _contained(claude, repo):
        rows.append((".claude/CLAUDE.md", ".claude/CLAUDE.md"))
    for prefix, patterns in (
        (".claude/rules", ("**/*.md",)),
        (".cursor/rules", ("**/*.md", "**/*.mdc")),
        (".agents/rules", ("**/*.md",)),
    ):
        for relative in _rule_relatives(repo, prefix, patterns):
            rows.append((relative, relative))
    return rows


def found_user_rules(home: Path) -> list[tuple[str, str]]:
    """`(listed, ~/ path)` for instruction files in the home directory."""
    rows: list[tuple[str, str]] = []
    named = (
        (".claude/CLAUDE.md", "user:CLAUDE.md"),
        (".codex/AGENTS.md", "user:.codex/AGENTS.md"),
        (".agents/AGENTS.md", "user:.agents/AGENTS.md"),
    )
    for relative, listed in named:
        path = home / relative
        if path.is_file() and _contained(path, home):
            rows.append((listed, _tilde(path, home)))
    for prefix, patterns in (
        (".claude/rules", ("**/*.md",)),
        (".cursor/rules", ("**/*.md", "**/*.mdc")),
    ):
        for relative in _rule_relatives(home, prefix, patterns):
            path = home / relative
            rows.append((_user_rule_listed(relative), _tilde(path, home)))
    return rows


def _all_rules(repo: Path, home: Path) -> list[RuleRef]:
    refs: list[RuleRef] = []
    for listed, path in found_project_rules(repo):
        refs.append(RuleRef(listed, "project", path, True))
    for listed, path in found_user_rules(home):
        refs.append(RuleRef(listed, "user", path, True))
    return refs


def _one_rule(repo: Path, home: Path, raw: str) -> RuleRef:
    if raw.startswith("user:"):
        relative = _user_rule_relative(raw.removeprefix("user:"))
        path = home / relative
        if not _contained(path, home):
            raise ConfigError(f'rule "{raw}" must be {_USER_RULE_HINT}')
        listed = _user_rule_listed(relative)
        display = _tilde(path, home) if path.is_file() else None
        return RuleRef(listed, "user", display, False)
    if not _project_rule_ok(raw):
        raise ConfigError(f'rule "{raw}" must be {_PROJECT_RULE_HINT}')
    path = repo / raw
    if not _contained(path, repo):
        raise ConfigError(f'rule "{raw}" must be {_PROJECT_RULE_HINT}')
    if not path.is_file():
        raise ConfigError(f'rule "{raw}" not found in the repository')
    return RuleRef(raw, "project", raw, False)


def _rules(cfg: Config, own: list[str], where: str, problems: list[str], home: Path) -> list[RuleRef]:
    rows: list[RuleRef] = []
    seen: set[str] = set()
    for name in list(cfg["rules"]["all"]) + [item for item in own if item not in cfg["rules"]["all"]]:
        listed_at = "[rules].all" if name in cfg["rules"]["all"] else f"{where}.rules"
        try:
            fresh = _all_rules(cfg.repo, home) if name == "all" else [_one_rule(cfg.repo, home, name)]
        except ConfigError as error:
            problem = f"{listed_at}: {error}"
            if problem not in problems:
                problems.append(problem)
            continue
        for ref in fresh:
            if ref.listed in seen:
                continue
            seen.add(ref.listed)
            rows.append(ref)
    return rows


@dataclass
class Agent:
    key: str                    # the key in the model tables: `build`, `reviewer-code`
    name: str                   # `mfactory-build`, or `mfactory-build-low` for a risk variant
    risk: str | None
    kind: str                   # a role, `custom`, `verify` or `reviewer`
    description: str
    roles: list                 # files under .factory/roles/ to read, in order
    sections: list = field(default_factory=list)   # (heading, text) from the project's config
    skills: list[SkillRef] = field(default_factory=list)
    rules: list[RuleRef] = field(default_factory=list)
    prompt: str | None = None   # repo-relative file listed after the role files


def _everything(cfg: Config, home: Path) -> list[SkillRef]:
    """Every project skill, user skill, and plugin skill the enabled tools can see."""
    refs: list[SkillRef] = []
    for name, where, _description in found_skills(cfg.repo):
        refs.append(SkillRef(name, "project", name, where, True))
    for name, where, _description in found_user_skills(home):
        refs.append(SkillRef(f"user:{name}", "user", name, where, True))
    seen: set[str] = set()
    for tool in cfg.tools:
        for listed, _path in found_plugin_skills(home, tool):
            if listed in seen:
                continue
            seen.add(listed)
            refs.append(SkillRef(listed, "plugin", listed, None, True))
    return refs


def _one_skill(repo: Path, home: Path, raw: str) -> SkillRef:
    kind, name = classify_skill(raw)
    if kind == "project":
        return SkillRef(raw, "project", name, str(resolve_skill(repo, name)), False)
    if kind == "user":
        found = _user_skill_path(home, name)
        return SkillRef(f"user:{name}", "user", name, _tilde(found, home) if found else None, False)
    return SkillRef(name, "plugin", name, None, False)


def _skills(cfg: Config, own: list[str], where: str, problems: list[str], home: Path) -> list[SkillRef]:
    rows: list[SkillRef] = []
    seen: set[str] = set()
    for name in list(cfg["skills"]["all"]) + [item for item in own if item not in cfg["skills"]["all"]]:
        listed_at = "[skills].all" if name in cfg["skills"]["all"] else f"{where}.skills"
        try:
            fresh = _everything(cfg, home) if name == "all" else [_one_skill(cfg.repo, home, name)]
        except ConfigError as error:
            problem = f"{listed_at}: {error}"
            if problem not in problems:
                problems.append(problem)
            continue
        for ref in fresh:
            if ref.listed in seen:
                continue
            seen.add(ref.listed)
            rows.append(ref)
    return rows


def _pull_request_title(cfg: Config, kind: str) -> str | None:
    """What build and intake are told about the pull request title, or None when both keys are empty."""
    project: dict[str, Any] = cfg["project"]
    example: str = str(project.get("pr_title", "")).strip()
    pattern: str = str(project.get("pr_title_pattern", "")).strip()
    if not example and not pattern:
        return None
    lines: list[str] = []
    if kind == "build":
        lines.append(
            "Open the pull request with a title in this form, not the spec title. "
            "If a pull request already exists and its title does not match, change it with "
            '`gh pr edit <number> --title "..."`.'
        )
    else:
        lines.append(
            "If the issue does not contain a value this title needs, such as a ticket key, "
            "return NEEDS-INFO and ask for it. Do not invent one."
        )
    if example:
        lines.append(f"Form: `{example}`.")
    if pattern:
        lines.append(f"The whole title must match `{pattern}`.")
    if project.get("merge_method") == "squash":
        lines.append("Squash merge uses this title as the commit on the base branch.")
    return "\n".join(lines)


def _pull_request_body(cfg: Config) -> str:
    """How build links the issue on the first line of the pull request body, from `[project].issue_link`."""
    if cfg["project"].get("issue_link") == "closes":
        return "The first line is `Closes #<issue>`. GitHub closes the issue when the pull request merges."
    return ("The first line is `Refs #<issue>`. Do not put a closing keyword (close, fix, resolve) "
            "next to the issue number: the issue stays open after the merge.")


def _prompt_file(settings: dict[str, Any], key: str) -> str | None:
    """The repo-relative prompt path on `settings`, or None when that key is unset."""
    raw: object = settings.get(key)
    if not isinstance(raw, str):
        return None
    text: str = raw.strip()
    return text or None


def agents(cfg: Config) -> list[Agent]:
    """Every agent the factory can start, one per risk variant."""
    out: list[Agent] = []
    problems: list[str] = []
    home: Path = skill_home()
    for key in cfg.agent_keys():
        prompt_file: str | None = None
        if key.startswith("reviewer-") and key.removeprefix("reviewer-") in cfg.reviewers:
            reviewer = key.removeprefix("reviewer-")
            settings = cfg.reviewers[reviewer]
            kind, where = "reviewer", f"[reviewers.{reviewer}]"
            roles = ["common.md", "reviewer.md"]
            if reviewer in BUILTIN_REVIEWERS:
                roles.append(f"reviewer-{reviewer}.md")
            description = (f"Factory reviewer {reviewer}. Reviews a job's branch and writes "
                           f"round-N/review-{reviewer}.md.")
            sections = [("Your review",
                         f"You are the `{reviewer}` reviewer. Write `round-<N>/review-{reviewer}.md`.")]
            if settings.get("focus"):
                sections.append(("What to look for", settings["focus"]))
            prompt_file = _prompt_file(settings, "prompt")
        else:
            settings = cfg.steps[key]
            where = f"[steps.{key}]"
            kind = cfg.role(key) if settings["type"] == "agent" else "verify"
            roles = ["common.md", "step.md" if kind == "custom" else f"{kind}.md"]
            description = f"Factory step {key}. {DESCRIPTIONS[kind]}"
            sections = []
            if kind == "intake":
                surfaces = "\n".join(f"- `{n}`: {s['when']}" for n, s in cfg.live_surfaces.items())
                sections.append(("This project", cfg["project"]["description"].strip()
                                 or "No description is set. Judge what belongs from AGENTS.md and the code."))
                sections.append(("Surfaces",
                                 "Name a surface on the `SURFACES:` line when the change touches it:\n\n"
                                 + (surfaces or "(none defined)") + "\n\nUse only these names, or `none`."))
            elif kind == "custom":
                writes = settings.get("writes") or f"{key}.md"
                task: str = (settings.get("instructions") or "").strip()
                if task:
                    sections.append(("Your task", task))
                sections.append(("Your file", f"Write `{writes}` in the job folder. Its first line must be "
                                 f"`STATUS: <OUTCOME>`, with OUTCOME one of: {', '.join(settings['on'])}."))
                prompt_file = _prompt_file(settings, "instructions_file")
            if key == "approve":
                merge_step: dict = cfg.steps.get("merge", {})
                who_merges: str
                if graph.enabled(merge_step):
                    who_merges = ("APPROVE means the factory merges after syncing the base branch "
                                  "and running the checks again.")
                else:
                    who_merges = "APPROVE asks a maintainer to merge. The factory does not merge."
                sections.append(("Who merges", who_merges))
            if kind in ("intake", "build"):
                title_text: str | None = _pull_request_title(cfg, kind)
                if title_text:
                    sections.append(("Pull request title", title_text))
            if kind == "build":
                sections.append(("Pull request body", _pull_request_body(cfg)))
        if settings.get("context", "").strip():
            sections.append(("Project context", settings["context"].strip()))
        skills = _skills(cfg, settings.get("skills", []), where, problems, home)
        rules = _rules(cfg, settings.get("rules", []), where, problems, home)
        for risk in [None] + cfg.variants(key):
            note = f" For {risk}-risk jobs." if risk else ""
            out.append(Agent(key, cfg.agent_name(key, risk), risk, kind, description + note, roles,
                             sections, skills, rules, prompt_file))
    if problems:
        raise ConfigError(problems)
    return out


def _plugins_for(tool: str, agent: Agent, index: SkillIndex) -> list[tuple[SkillRef, str | None]]:
    """Plugin skills this tool's prompt names, with a `~/` path when the tool has the files."""
    shown: list[tuple[SkillRef, str | None]] = []
    for skill in agent.skills:
        if skill.kind != "plugin":
            continue
        path = index.plugin_display(tool, skill.name)
        if skill.via_all and path is None:
            continue
        shown.append((skill, path))
    return shown


def _plugin_intro(tool: str) -> str:
    if tool == "claude":
        return "Invoke each of these with the Skill tool before you start, and follow it for this job:"
    if tool == "codex":
        return "Follow each of these before you start by mentioning it, and apply it to this job:"
    return "Use each of these skills by name before you start, and follow it for this job:"


def body(cfg: Config, tool: str, agent: Agent, index: SkillIndex | None = None) -> str:
    """The instructions every tool's agent file carries."""
    index = index or SkillIndex(skill_home())
    lines = ["You are one agent of this project's software factory. Your prompt names the main checkout.",
             "Read these files there, in this order, and follow them exactly:", ""]
    lines += [f"{i}. `.factory/roles/{name}`" for i, name in enumerate(agent.roles, 1)]
    if agent.prompt:
        lines.append(f"{len(agent.roles) + 1}. `{agent.prompt}`")
    if agent.rules:
        lines += ["", "## Instruction files", "",
                  "Read each of these before you start, and follow it for this job:", ""]
        for rule in agent.rules:
            lines.append(f"- `{rule.path}`" if rule.path else f"- `{rule.listed}`")
    project = [skill for skill in agent.skills if skill.kind == "project"]
    user = [skill for skill in agent.skills if skill.kind == "user"]
    plugins = _plugins_for(tool, agent, index)
    if project:
        lines += ["", "## Project skills", "",
                  "Read each of these in the main checkout before you start, and apply it to this job:", ""]
        lines += [f"- `{skill.name}`: `{skill.path}`" for skill in project]
    if user:
        lines += ["", "## User skills", "",
                  "Read each of these on this machine before you start, and apply it to this job:", ""]
        for skill in user:
            if skill.path:
                lines.append(f"- `{skill.name}`: `{skill.path}`")
            else:
                lines.append(f"- `{skill.name}`")
    if plugins:
        lines += ["", "## Plugin skills", "", _plugin_intro(tool), ""]
        for skill, path in plugins:
            mention = f"${skill.name}" if tool == "codex" else skill.name
            if path:
                lines.append(f"- `{mention}`: `{path}`")
            else:
                lines.append(f"- `{mention}`")
    for heading, text in agent.sections:
        lines += ["", f"## {heading}", "", text]
    if agent.kind == "intake" and cfg.adapters[tool]["agents"]["intake"] == "reply":
        lines += ["", "## Handing in your file", "",
                  "You can't write files or run commands here. Instead of writing `intake.md`, reply with",
                  "its full contents and nothing else, starting with the `DECISION:` line. The factory",
                  "saves your reply as `intake.md`. This replaces the one-line reply other agents give."]
    return "\n".join(lines) + "\n"


# ---- One renderer per tool -------------------------------------------------


def _claude_tools(agent: Agent, index: SkillIndex) -> str:
    tools = CLAUDE_TOOLS.get(agent.kind, CLAUDE_TOOLS_DEFAULT)
    if _plugins_for("claude", agent, index) and "Skill" not in [part.strip() for part in tools.split(",")]:
        tools = f"{tools}, Skill"
    return tools


def _claude(cfg, agent, model, text, index: SkillIndex | None = None):
    index = index or SkillIndex(skill_home())
    adapter = cfg.adapters["claude"]
    front = [f"name: {agent.name}", f"description: {json.dumps(agent.description)}",
             f"tools: {_claude_tools(agent, index)}", f"model: {model['model']}"]
    if model.get("effort"):
        front.append(f"effort: {model['effort']}")
    preload_root = adapter["paths"]["skills_preload"] + "/"
    preload = [skill.name for skill in agent.skills
               if skill.kind == "project" and skill.path and skill.path.startswith(preload_root)]
    if preload:
        front += ["skills:"] + [f"  - {name}" for name in preload]
    if agent.kind == "build" and cfg["tools"].get("claude", {}).get("stop_hook"):
        front += ["hooks:", "  Stop:", "    - hooks:", "        - type: command",
                  "          command: '\"$CLAUDE_PROJECT_DIR\"/.factory/bin/mfactory hook'"]
    return f"{agent.name}.md", "---\n" + "\n".join(front) + f"\n---\n<!-- {NOTE} -->\n\n{text}"


def _cursor(cfg, agent, model, text):
    name = model["model"]
    if model.get("effort") and name != "inherit":
        name += f"[effort={model['effort']}]"
    front = [f"name: {agent.name}", f"description: {json.dumps(agent.description)}", f"model: {name}"]
    if agent.kind == "intake":
        front.append("readonly: true")
    return f"{agent.name}.md", "---\n" + "\n".join(front) + f"\n---\n<!-- {NOTE} -->\n\n{text}"


def _codex(cfg, agent, model, text):
    escaped = text.replace("\\", "\\\\").replace('"""', '""\\"')
    lines = [f"# {NOTE}", f"name = {json.dumps(agent.name)}", f"description = {json.dumps(agent.description)}"]
    if model["model"] != "inherit":
        lines.append(f"model = {json.dumps(model['model'])}")
    if model.get("effort"):
        lines.append(f"model_reasoning_effort = {json.dumps(model['effort'])}")
    lines.append(f'developer_instructions = """\n{escaped}"""')
    if agent.kind == "intake" and not cfg["tools"].get("codex", {}).get("intake_shell"):
        lines += ["", "[features]", "shell_tool = false"]
    return f"{agent.name}.toml", "\n".join(lines) + "\n"


RENDERERS = {"claude": _claude, "cursor": _cursor, "codex": _codex}


def skill_text():
    """The orchestrator skill, marked as generated right after its frontmatter."""
    text = (CORE / "skill" / "SKILL.md").read_text()
    head, _, rest = text.removeprefix("---\n").partition("\n---\n")
    return f"---\n{head}\n---\n<!-- {NOTE} -->\n{rest}"


def skill_dirs(cfg):
    """One copy of the skill per tool, skipping a tool that already reads another's folder."""
    written = []
    for tool in sorted(cfg.tools, key=lambda t: len(cfg.adapters[t]["paths"]["reads_skills"])):
        paths = cfg.adapters[tool]["paths"]
        if not any(str(Path(w).parent) in paths["reads_skills"] for w in written):
            written.append(paths["skill"])
    return written


def skill_gap_notes(cfg: Config, listed: list[Agent] | None = None) -> list[str]:
    """User and plugin skills named in the config that this machine cannot see.

    `all` expands to skills that are already here, so it adds no gap.
    """
    if listed is None:
        listed = agents(cfg)
    index = SkillIndex(skill_home())
    notes: list[str] = []
    seen: set[str] = set()
    for agent in listed:
        if agent.risk is not None:
            continue
        for skill in agent.skills:
            if skill.via_all:
                continue
            if skill.kind == "user" and skill.path is None:
                text = f'skill "{skill.listed}" is not installed on this machine'
                if text not in seen:
                    seen.add(text)
                    notes.append(text)
            elif skill.kind == "plugin":
                for tool in cfg.tools:
                    if index.plugin_path(tool, skill.name) is not None:
                        continue
                    line = f'skill "{skill.listed}" is not installed for {cfg.adapters[tool]["label"]}'
                    if line not in seen:
                        seen.add(line)
                        notes.append(line)
        for rule in agent.rules:
            if rule.via_all or rule.kind != "user" or rule.path is not None:
                continue
            text = f'rule "{rule.listed}" is not installed on this machine'
            if text not in seen:
                seen.add(text)
                notes.append(text)
    return notes


def plan(cfg: Config) -> tuple[dict[str, str], list[str]]:
    """Every file sync writes, as {path relative to the repo: text}, plus notes for the user."""
    files: dict[str, str] = {}
    notes: list[str] = []
    listed = agents(cfg)
    index = SkillIndex(skill_home())
    for tool in cfg.tools:
        adapter = cfg.adapters[tool]
        for agent in listed:
            model = cfg.model_for(tool, agent.key, agent.risk)
            if not model.get("model"):
                raise ConfigError(f"[models.{tool}].{agent.key} has no model, and [models.{tool}].default has none")
            if tool == "cursor" and model.get("effort") and model["model"] == "inherit":
                notes.append(f"[models.cursor].{agent.key}: Cursor sets effort only on a named model, "
                             f'so effort = "{model["effort"]}" is ignored while model = "inherit"')
            rendered = body(cfg, tool, agent, index)
            renderer = RENDERERS[adapter["agents"]["format"]]
            if adapter["agents"]["format"] == "claude":
                name, text = renderer(cfg, agent, model, rendered, index)
            else:
                name, text = renderer(cfg, agent, model, rendered)
            files[f"{adapter['paths']['agents']}/{name}"] = text
    notes.extend(skill_gap_notes(cfg, listed))
    dirs = skill_dirs(cfg)
    for folder in dirs:
        files[f"{folder}/SKILL.md"] = skill_text()
    if "cursor" in cfg.tools:
        seen = [d for d in dirs if str(Path(d).parent) in cfg.adapters["cursor"]["paths"]["reads_skills"]]
        if len(seen) > 1:
            notes.append(f"Cursor reads the factory skill from {' and '.join(seen)}, so it may list it twice. "
                         "The copies are identical.")
        elif seen and seen[0] != cfg.adapters["cursor"]["paths"]["skill"]:
            notes.append(f"Cursor uses the factory skill in {seen[0]}, which it loads alongside its own folders.")
    return files, sorted(set(notes))


def stale(cfg, files):
    """Generated files left over from an earlier config, in any tool's folders.

    Also removes files named `factory-*` from before the mfactory rename.
    """
    out = []
    seen = set()
    for adapter in cfg.adapters.values():
        folder = cfg.repo / adapter["paths"]["agents"]
        for pattern in ("mfactory-*", "factory-*"):
            for path in sorted(folder.glob(pattern)):
                rel = str(path.relative_to(cfg.repo))
                if rel in seen or rel in files or not path.is_file():
                    continue
                if is_generated(path.read_text(errors="replace")):
                    seen.add(rel)
                    out.append(rel)
    for folder in SKILL_DIRS:
        for name in ("mfactory", "factory"):
            path = cfg.repo / folder / name / "SKILL.md"
            rel = str(path.relative_to(cfg.repo))
            if rel in seen or rel in files or not path.is_file():
                continue
            if is_generated(path.read_text(errors="replace")):
                seen.add(rel)
                out.append(rel)
    return out


def sync(cfg, check=False):
    """Write what differs and remove what is stale. Returns ([(action, path)], notes)."""
    files, notes = plan(cfg)
    actions = []
    for rel, text in files.items():
        path = cfg.repo / rel
        current = path.read_text() if path.is_file() else None
        if current == text:
            continue
        actions.append(("update" if current is not None else "create", rel))
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    for rel in stale(cfg, files):
        actions.append(("remove", rel))
        if not check:
            path = cfg.repo / rel
            path.unlink()
            if not any(path.parent.iterdir()):
                path.parent.rmdir()
    return actions, notes


def describe_models(cfg):
    """The model and effort each agent runs on, per enabled tool."""
    rows = [("agent",) + tuple(cfg.tools)]
    for agent in agents(cfg):
        cells = []
        for tool in cfg.tools:
            m = cfg.model_for(tool, agent.key, agent.risk)
            cells.append(m.get("model", "?") + (f" / {m['effort']}" if m.get("effort") else ""))
        rows.append((agent.name,) + tuple(cells))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows) + "\n"


def describe_skills(cfg: Config) -> str:
    """`mfactory skills`: project, user and plugin skills, and which agents read each."""
    home = skill_home()
    index = SkillIndex(home)
    roster = [agent for agent in agents(cfg) if agent.risk is None]
    every = len(roster)
    readers: dict[str, list[str]] = {}
    listed_user: list[str] = []
    listed_plugin: list[str] = []
    for agent in roster:
        short = agent.name.removeprefix("mfactory-").removeprefix("factory-")
        for skill in agent.skills:
            readers.setdefault(skill.listed, []).append(short)
            if skill.kind == "user" and skill.name not in listed_user:
                listed_user.append(skill.name)
            elif skill.kind == "plugin" and skill.listed not in listed_plugin:
                listed_plugin.append(skill.listed)
        for rule in agent.rules:
            readers.setdefault(rule.listed, []).append(short)

    def used(listed: str) -> str:
        who = list(dict.fromkeys(readers.get(listed, [])))
        if who and len(who) == every:
            return "every agent"
        return ", ".join(who) or "no agent"

    def entry(listed: str, where: str, description: str = "") -> str:
        line = f'{listed}  {where}\n    skills = ["{listed}"]\n    read by: {used(listed)}'
        if description:
            line += f"\n    {description[:100]}"
        return line

    lines: list[str] = ["project"]
    found = found_skills(cfg.repo)
    if found:
        lines.extend(entry(name, where, description) for name, where, description in found)
    else:
        lines.append(f"  (none in {', '.join(SKILL_DIRS)})")
    lines.append("")
    lines.append("user")
    user_rows: list[str] = []
    seen_user: set[str] = set()
    for name, where, description in found_user_skills(home):
        seen_user.add(name)
        user_rows.append(entry(f"user:{name}", where, description))
    for name in listed_user:
        if name not in seen_user:
            user_rows.append(entry(f"user:{name}", "not installed"))
    lines.extend(user_rows or ["  (none)"])
    lines.append("")
    lines.append("plugin")
    plugin_ids: list[str] = []
    for tool in cfg.tools:
        for listed, _path in found_plugin_skills(home, tool):
            if listed not in plugin_ids:
                plugin_ids.append(listed)
    for listed in listed_plugin:
        if listed not in plugin_ids:
            plugin_ids.append(listed)
    if not plugin_ids:
        lines.append("  (none)")
    else:
        for listed in plugin_ids:
            places = [f"    {cfg.adapters[tool]['label']}: {index.plugin_display(tool, listed) or 'not installed'}"
                      for tool in cfg.tools]
            lines.append(f'{listed}\n    skills = ["{listed}"]\n    read by: {used(listed)}\n' + "\n".join(places))
    lines.append("")
    lines.append("instructions")
    instruction_rows: list[str] = []
    seen_rules: set[str] = set()
    listed_missing: list[str] = []
    for agent in roster:
        for rule in agent.rules:
            if rule.kind == "user" and rule.path is None and rule.listed not in listed_missing:
                listed_missing.append(rule.listed)

    def rule_entry(listed: str, where: str) -> str:
        return f'{listed}  {where}\n    rules = ["{listed}"]\n    read by: {used(listed)}'

    for listed, where in found_project_rules(cfg.repo) + found_user_rules(home):
        seen_rules.add(listed)
        instruction_rows.append(rule_entry(listed, where))
    for listed in listed_missing:
        if listed not in seen_rules:
            instruction_rows.append(rule_entry(listed, "not installed"))
    lines.extend(instruction_rows or ["  (none)"])
    lines.append("")
    lines.append("Set who reads a skill in factory.toml: [skills].all, or `skills = [...]` under a step or reviewer.")
    lines.append("`all` in that list reads every project skill, user skill, and plugin skill on this machine.")
    lines.append("Set who reads an instruction file in factory.toml: [rules].all, "
                 "or `rules = [...]` under a step or reviewer.")
    lines.append("`all` in that list reads every AGENTS.md, CLAUDE.md, and rule file in the repo and on this machine.")
    return "\n".join(lines) + "\n"
