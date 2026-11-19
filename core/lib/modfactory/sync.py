"""`factory sync`: write each enabled tool's agent files and the orchestrator skill from factory.toml.

The prompts live once, in `.factory/roles/`. A generated agent file holds only
what differs per tool (name, model, effort, permissions) and what comes from
the project's config (skills, context, custom instructions).
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import BUILTIN_REVIEWERS
from .util import CORE, GENERATED, ConfigError

SKILL_DIRS = [".agents/skills", ".claude/skills", ".cursor/skills", ".codex/skills"]
NOTE = f"{GENERATED}: change factory.toml, then run `.factory/bin/factory sync`."

DESCRIPTIONS = {
    "intake": "Decides whether an issue is worth building and at what scope, and writes intake.md.",
    "spec": "Turns an accepted issue into spec.md with testable acceptance criteria.",
    "build": "Implements the spec on the job's branch, opens the pull request, and writes build.md.",
    "debug": "Finds why the project's checks fail and writes a diagnosis. Never changes code.",
    "approve": "Decides whether the job's pull request merges or goes to a person.",
    "verify": "Checks the merged change against the spec's acceptance criteria.",
    "custom": "A step this project defines in factory.toml.",
}
CLAUDE_TOOLS = {
    "intake": "Read, Grep, Glob, Write",
    "spec": "Read, Grep, Glob, Bash, Write, Skill",
    "build": "Read, Grep, Glob, Bash, Edit, Write, Skill",
    "custom": "Read, Grep, Glob, Bash, Edit, Write, Skill",
}
CLAUDE_TOOLS_DEFAULT = "Read, Grep, Glob, Bash, Write, Skill"


# ---- Project skills --------------------------------------------------------


def resolve_skill(repo, name):
    for folder in SKILL_DIRS:
        path = Path(repo) / folder / name / "SKILL.md"
        if path.is_file():
            return path.relative_to(repo)
    raise ConfigError(f'skill "{name}" not found in {", ".join(SKILL_DIRS)}')


def found_skills(repo):
    """Every project skill on disk, as (name, path, description), without the factory's own."""
    seen, rows = set(), []
    for folder in SKILL_DIRS:
        for path in sorted((Path(repo) / folder).glob("*/SKILL.md")):
            name = path.parent.name
            if name == "factory" or name in seen:
                continue
            seen.add(name)
            m = re.search(r"^description:\s*(.+)$", path.read_text(errors="replace"), re.M)
            rows.append((name, str(path.relative_to(repo)), m.group(1).strip().strip("\"'") if m else ""))
    return rows


# ---- Agents, independent of the tool ---------------------------------------


@dataclass
class Agent:
    key: str                    # the key in the model tables: `build`, `reviewer-code`
    name: str                   # `factory-build`, or `factory-build-low` for a risk variant
    risk: str | None
    kind: str                   # a role, `custom`, `verify` or `reviewer`
    description: str
    roles: list                 # files under .factory/roles/ to read, in order
    sections: list = field(default_factory=list)   # (heading, text) from the project's config
    skills: list = field(default_factory=list)     # (name, path relative to the repo)


def _skills(cfg, own, where, problems):
    rows = []
    for name in list(cfg["skills"]["all"]) + [s for s in own if s not in cfg["skills"]["all"]]:
        try:
            rows.append((name, str(resolve_skill(cfg.repo, name))))
        except ConfigError as e:
            listed = "[skills].all" if name in cfg["skills"]["all"] else f"{where}.skills"
            problems.append(f"{listed}: {e}")
    return rows


def agents(cfg):
    """Every agent the factory can start, one per risk variant."""
    out, problems = [], []
    for key in cfg.agent_keys():
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
        else:
            settings = cfg.steps[key]
            where = f"[steps.{key}]"
            kind = cfg.role(key) if settings["type"] == "agent" else "verify"
            roles = ["common.md", "step.md" if kind == "custom" else f"{kind}.md"]
            description = f"Factory step {key}. {DESCRIPTIONS[kind]}"
            sections = []
            if kind == "intake":
                surfaces = "\n".join(f"- `{n}`: {s['when']}" for n, s in cfg["surfaces"].items())
                sections.append(("This project", cfg["project"]["description"].strip()
                                 or "No description is set. Judge what belongs from AGENTS.md and the code."))
                sections.append(("Surfaces",
                                 "Name a surface on the `SURFACES:` line when the change touches it:\n\n"
                                 + (surfaces or "(none defined)") + "\n\nUse only these names, or `none`."))
            elif kind == "custom":
                writes = settings.get("writes") or f"{key}.md"
                sections.append(("Your task", settings["instructions"].strip()))
                sections.append(("Your file", f"Write `{writes}` in the job folder. Its first line must be "
                                 f"`STATUS: <OUTCOME>`, with OUTCOME one of: {', '.join(settings['on'])}."))
        if settings.get("context", "").strip():
            sections.append(("Project context", settings["context"].strip()))
        skills = _skills(cfg, settings.get("skills", []), where, problems)
        for risk in [None] + cfg.variants(key):
            note = f" For {risk}-risk jobs." if risk else ""
            out.append(Agent(key, cfg.agent_name(key, risk), risk, kind, description + note, roles,
                             sections, skills))
    if problems:
        raise ConfigError(problems)
    return out


def body(cfg, tool, agent):
    """The instructions every tool's agent file carries."""
    lines = ["You are one agent of this project's software factory. Your prompt names the main checkout.",
             "Read these files there, in this order, and follow them exactly:", ""]
    lines += [f"{i}. `.factory/roles/{name}`" for i, name in enumerate(agent.roles, 1)]
    if agent.skills:
        lines += ["", "## Project skills", "",
                  "Read each of these in the main checkout before you start, and apply it to this job:", ""]
        lines += [f"- `{name}`: `{path}`" for name, path in agent.skills]
    for heading, text in agent.sections:
        lines += ["", f"## {heading}", "", text]
    if agent.kind == "intake" and cfg.adapters[tool]["agents"]["intake"] == "reply":
        lines += ["", "## Handing in your file", "",
                  "You can't write files or run commands here. Instead of writing `intake.md`, reply with",
                  "its full contents and nothing else, starting with the `DECISION:` line. The factory",
                  "saves your reply as `intake.md`. This replaces the one-line reply other agents give."]
    return "\n".join(lines) + "\n"


# ---- One renderer per tool -------------------------------------------------


def _claude(cfg, agent, model, text):
    adapter = cfg.adapters["claude"]
    front = [f"name: {agent.name}", f"description: {json.dumps(agent.description)}",
             f"tools: {CLAUDE_TOOLS.get(agent.kind, CLAUDE_TOOLS_DEFAULT)}", f"model: {model['model']}"]
    if model.get("effort"):
        front.append(f"effort: {model['effort']}")
    preload = [name for name, path in agent.skills if path.startswith(adapter["paths"]["skills_preload"] + "/")]
    if preload:
        front += ["skills:"] + [f"  - {name}" for name in preload]
    if agent.kind == "build" and cfg["tools"].get("claude", {}).get("stop_hook"):
        front += ["hooks:", "  Stop:", "    - hooks:", "        - type: command",
                  "          command: '\"$CLAUDE_PROJECT_DIR\"/.factory/bin/factory hook'"]
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


def plan(cfg):
    """Every file sync writes, as {path relative to the repo: text}, plus notes for the user."""
    files, notes = {}, []
    listed = agents(cfg)
    for tool in cfg.tools:
        adapter = cfg.adapters[tool]
        for agent in listed:
            model = cfg.model_for(tool, agent.key, agent.risk)
            if not model.get("model"):
                raise ConfigError(f"[models.{tool}].{agent.key} has no model, and [models.{tool}].default has none")
            if tool == "cursor" and model.get("effort") and model["model"] == "inherit":
                notes.append(f"[models.cursor].{agent.key}: Cursor sets effort only on a named model, "
                             f'so effort = "{model["effort"]}" is ignored while model = "inherit"')
            name, text = RENDERERS[adapter["agents"]["format"]](cfg, agent, model, body(cfg, tool, agent))
            files[f"{adapter['paths']['agents']}/{name}"] = text
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
    """Generated files left over from an earlier config, in any tool's folders."""
    out = []
    for adapter in cfg.adapters.values():
        for path in sorted((cfg.repo / adapter["paths"]["agents"]).glob("factory-*")):
            rel = str(path.relative_to(cfg.repo))
            if rel not in files and path.is_file() and GENERATED in path.read_text(errors="replace"):
                out.append(rel)
    for folder in SKILL_DIRS:
        path = cfg.repo / folder / "factory" / "SKILL.md"
        rel = str(path.relative_to(cfg.repo))
        if rel not in files and path.is_file() and GENERATED in path.read_text(errors="replace"):
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


def describe_skills(cfg):
    """`factory skills`: the project's skills on disk and the agents that read each."""
    readers = {}
    for agent in agents(cfg):
        if agent.risk is None:
            for name, _ in agent.skills:
                readers.setdefault(name, []).append(agent.name.removeprefix("factory-"))
    found = found_skills(cfg.repo)
    if not found:
        return f"No project skills found in {', '.join(SKILL_DIRS)}.\n"
    every = len([a for a in agents(cfg) if a.risk is None])
    lines = []
    for name, where, description in found:
        who = readers.get(name, [])
        used = "every agent" if len(who) == every else (", ".join(who) or "no agent")
        lines.append(f"{name}  {where}\n    read by: {used}" + (f"\n    {description[:100]}" if description else ""))
    lines.append("")
    lines.append("Set who reads a skill in factory.toml: [skills].all, or `skills = [...]` under a step or reviewer.")
    return "\n".join(lines) + "\n"
