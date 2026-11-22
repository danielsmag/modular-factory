"""factory.toml: load it over the defaults, validate it, and say where each value came from."""

import json
import re
import tomllib
from pathlib import Path

from . import graph
from .util import CORE, ConfigError, Refused, adapters_dir

SCHEMA = 1
FILE = "factory.toml"
NAME = re.compile(r"[a-z][a-z0-9-]*")
RISKS = ("low", "medium", "high")
RESERVED = graph.END_STATES | {"default"}


# ---- Loading and merging ---------------------------------------------------


def read_toml(path, label=None):
    try:
        return tomllib.loads(Path(path).read_text())
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{label or path}: {e}") from e


def merge(base, override):
    """Tables merge key by key; any other value replaces what was there."""
    out = dict(base)
    for key, value in override.items():
        both_tables = isinstance(value, dict) and isinstance(out.get(key), dict)
        out[key] = merge(out[key], value) if both_tables else value
    return out


def normalise_edges(layer):
    """Write every edge as a table, so `FAIL = "build"` and `FAIL = { max = 3 }` both merge."""
    steps = layer.get("steps")
    if not isinstance(steps, dict):
        return layer
    out = dict(layer, steps={})
    for name, step in steps.items():
        if isinstance(step, dict) and isinstance(step.get("on"), dict):
            edges = {k: {"to": v} if isinstance(v, str) else v for k, v in step["on"].items()}
            step = dict(step, on=edges)
        out["steps"][name] = step
    return out


def leaves(table, prefix=()):
    for key, value in table.items():
        if isinstance(value, dict) and value:
            yield from leaves(value, prefix + (key,))
        else:
            yield prefix + (key,), value


def load_adapters():
    adapters = {}
    for path in sorted(adapters_dir().glob("*.toml")):
        data = read_toml(path)
        adapters[data["name"]] = data
    return adapters


class Config:
    def __init__(self, repo, data, sources, adapters):
        self.repo = Path(repo)
        self.data = data
        self.sources = sources
        self.adapters = adapters

    def __getitem__(self, key):
        return self.data[key]

    # -- shortcuts

    @property
    def steps(self):
        return self.data["steps"]

    @property
    def reviewers(self):
        return self.data["reviewers"]

    @property
    def tools(self):
        return self.data["tools"]["enabled"]

    @property
    def jobs_dir(self):
        return self.repo / ".factory" / "jobs"

    @property
    def base(self):
        return self.data["project"]["base_branch"]

    def role(self, step):
        return graph.role(step, self.steps[step])

    # -- agents and models

    def agent_keys(self, every=False):
        """The agents the factory can start: agent steps, verifier agents and reviewers.

        These are also the keys of the model tables. `every` includes the ones
        that are switched off, which may still have a model set.
        """
        keys = []
        for name, step in self.steps.items():
            if not every and not graph.enabled(step):
                continue
            if step.get("type") == "agent" or (step.get("type") == "verify" and (every or step.get("agent"))):
                keys.append(name)
        return keys + [f"reviewer-{name}" for name in self.reviewers]

    def variants(self, key):
        """Risk levels for which any enabled tool gives this agent its own model or effort."""
        found = set()
        for tool in self.tools:
            entry = self.data["models"].get(tool, {}).get(key, {})
            found |= {risk for risk in RISKS if risk in entry}
        return [risk for risk in RISKS if risk in found]

    def agent_name(self, key, risk=None):
        return f"mfactory-{key}-{risk}" if risk in self.variants(key) else f"mfactory-{key}"

    def model_for(self, tool, key, risk=None):
        """The step's entry over the tool's `default`, key by key, then the entry for the job's risk."""
        table = self.data["models"].get(tool, {})
        entry = table.get(key) or {}
        chosen = {k: v for k, v in (table.get("default") or {}).items() if k in ("model", "effort")}
        chosen.update({k: v for k, v in entry.items() if k in ("model", "effort")})
        if risk and isinstance(entry.get(risk), dict):
            chosen.update(entry[risk])
        return chosen

    def reviewers_for(self, surfaces):
        """Reviewers no surface claims run on every job; a surface adds its own."""
        claimed = {r for s in self.data["surfaces"].values() for r in s.get("reviewers", [])}
        chosen = [r for r in self.reviewers if r not in claimed]
        for surface in surfaces:
            chosen += [r for r in self.data["surfaces"][surface]["reviewers"] if r not in chosen]
        return chosen


def shown_agent(name):
    """The agent to start. Jobs saved before the rename stored `factory-` names."""
    if isinstance(name, str) and name.startswith("factory-"):
        return "m" + name
    return name


def load(repo, validate=True, text=None):
    """The effective config: defaults, then each adapter's model table, then factory.toml."""
    repo = Path(repo)
    path = repo / FILE
    if text is None and not path.exists():
        raise Refused(f"no {FILE} in {repo}. Run `mfactory install` there first.")
    adapters = load_adapters()
    layers = [("default", read_toml(CORE / "defaults.toml"))]
    for name, adapter in adapters.items():
        layers.append((f"adapter {name}", {"models": {name: adapter.get("models", {})}}))
    project = read_toml(path, FILE) if text is None else _parse(text)
    layers.append((FILE, project))

    data, sources = {}, {}
    for label, layer in layers:
        layer = normalise_edges(layer)
        data = merge(data, layer)
        for key_path, _ in leaves(layer):
            # A table replaced by a scalar, or the reverse, drops the sources below it.
            for old in [p for p in sources if p[:len(key_path)] == key_path or key_path[:len(p)] == p]:
                del sources[old]
            sources[key_path] = label
    cfg = Config(repo, data, sources, adapters)
    if validate:
        problems = check(cfg)
        if problems:
            raise ConfigError(problems)
    return cfg


def _parse(text):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{FILE}: {e}") from e


# ---- Validation ------------------------------------------------------------


def a_string(v):
    return None if isinstance(v, str) else "must be a string"


def a_filled_string(v):
    return None if isinstance(v, str) and v.strip() else "must be a non-empty string"


def a_bool(v):
    return None if isinstance(v, bool) else "must be true or false"


def a_string_list(v):
    ok = isinstance(v, list) and all(isinstance(i, str) and i.strip() for i in v)
    return None if ok else "must be a list of non-empty strings"


def an_int(minimum):
    def test(v):
        ok = isinstance(v, int) and not isinstance(v, bool) and v >= minimum
        return None if ok else f"must be a whole number of at least {minimum}"
    return test


def one_of(*options):
    def test(v):
        return None if v in options else "must be one of " + ", ".join(f'"{o}"' for o in options)
    return test


def a_regex(value: object) -> str | None:
    """A string that is empty or a compiling regular expression. Empty means the check is off."""
    if not isinstance(value, str):
        return "must be a string"
    pattern: str = value.strip()
    if not pattern:
        return None
    try:
        re.compile(pattern)
    except re.error as error:
        return f"must be a valid regular expression ({error})"
    return None


# GitHub login: 1–39 letters, digits, or hyphens, not starting or ending with a hyphen.
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
WHO_WORDS = ("author", "maintainer", "operator")


def a_who(value: object) -> str | None:
    """Who may reply on GitHub: a role, or a non-empty list of logins."""
    if isinstance(value, str) and value in WHO_WORDS:
        return None
    if isinstance(value, list) and value and all(isinstance(item, str) and LOGIN.fullmatch(item) for item in value):
        return None
    return 'must be "author", "maintainer", "operator", or a list of GitHub logins'


TABLES = {
    "project": {"description": a_string, "base_branch": a_filled_string,
                "merge_method": one_of("merge", "squash", "rebase"),
                "pr_title": a_string, "pr_title_pattern": a_regex},
    "run": {"concurrency": an_int(1), "start": a_filled_string, "escalate_to": a_filled_string},
    "fleet": {"concurrency": an_int(1)},
    "checks": {"commands": a_string_list, "timeout_minutes": an_int(1)},
    "labels": {"queue": a_filled_string, "needs_info": a_filled_string, "needs_human": a_filled_string},
    "paths": {"worktrees": a_filled_string, "protected": a_string_list},
    "ports": {"base": an_int(1024), "step": an_int(3)},
    "skills": {"all": a_string_list},
}
TOOL_KEYS = {"claude": {"stop_hook": a_bool, "cost": a_bool}, "codex": {"intake_shell": a_bool}}
STEP_COMMON = {"type": one_of(*graph.TYPES), "enabled": a_bool, "skip": a_filled_string}
STEP_KEYS = {
    "agent": {"role": one_of(*graph.ROLE_OUTCOMES, "custom"), "instructions": a_filled_string,
              "writes": a_filled_string, "skills": a_string_list, "context": a_string, "port": a_bool},
    "checks": {},
    "review": {},
    "human": {"who": a_who, "shows": a_string, "ask": a_string, "pr_ready": a_bool,
              "replies": one_of("any", "tool")},
    "merge": {},
    "verify": {"after": one_of("merge", "deploy"), "commands": a_string_list, "agent": a_bool,
               "retries": an_int(0), "on_failure": one_of("comment", "issue", "revert-pr"),
               "wait": a_string, "timeout_minutes": an_int(1), "url": a_string,
               "skills": a_string_list, "context": a_string, "port": a_bool},
}
REVIEWER_KEYS = {"focus": a_filled_string, "context": a_string, "skills": a_string_list, "port": a_bool}
BUILTIN_REVIEWERS = ("code", "security", "ux", "ui")
TOP = set(TABLES) | {"schema", "tools", "steps", "reviewers", "surfaces", "models"}


def check(cfg):
    """Every problem in the config, each naming the key to fix."""
    d, out = cfg.data, []

    def test_table(label, table, spec):
        if not isinstance(table, dict):
            out.append(f"[{label}] must be a table")
            return
        for key, value in table.items():
            if key not in spec:
                out.append(f"[{label}].{key} is not a setting. Known: {', '.join(sorted(spec)) or 'none'}")
            elif (problem := spec[key](value)):
                out.append(f"[{label}].{key} {problem} (got {json.dumps(value)})")

    for key in d:
        if key not in TOP:
            out.append(f"[{key}] is not a section. Known: {', '.join(sorted(TOP))}")
    if d.get("schema") != SCHEMA:
        out.append(f"schema = {d.get('schema')!r}, but this factory reads schema {SCHEMA}. "
                   "Run `mfactory upgrade`, or set schema to match.")
    for name, spec in TABLES.items():
        test_table(name, d.get(name, {}), spec)
    if cfg.sources.get(("fleet", "concurrency")) == FILE:
        out.append("[fleet].concurrency is the cap across projects. Set it in fleet.toml, "
                   "or pass --concurrency to `mfactory fleet queue`.")

    # tools
    tools = d.get("tools", {})
    enabled = tools.get("enabled")
    if a_string_list(enabled) or not enabled:
        out.append("[tools].enabled must list at least one of: " + ", ".join(cfg.adapters))
        enabled = []
    for tool in enabled:
        if tool not in cfg.adapters:
            out.append(f'[tools].enabled has "{tool}", which is not a tool. Known: {", ".join(cfg.adapters)}')
    for key, value in tools.items():
        if key == "enabled":
            continue
        if key not in cfg.adapters:
            out.append(f"[tools.{key}] is not a tool. Known: {', '.join(cfg.adapters)}")
        else:
            test_table(f"tools.{key}", value, TOOL_KEYS.get(key, {}))

    # steps
    steps = d.get("steps", {})
    for name, step in steps.items():
        label = f"steps.{name}"
        if not NAME.fullmatch(name) or name in RESERVED:
            out.append(f"[{label}]: a step name is lowercase letters, digits and hyphens, "
                       f"and not one of {', '.join(sorted(RESERVED))}")
        if not isinstance(step, dict) or step.get("type") not in graph.TYPES:
            out.append(f"[{label}].type must be one of: {', '.join(graph.TYPES)}")
            continue
        spec = STEP_COMMON | STEP_KEYS[step["type"]] | {"on": lambda v: None}
        test_table(label, step, spec)
        if step["type"] == "agent" and graph.role(name, step) == "custom" and not step.get("instructions"):
            out.append(f"[{label}].instructions is missing: say what this step's agent must do")
        if step["type"] == "verify" and graph.enabled(step):
            if not step.get("commands") and not step.get("agent"):
                out.append(f"[{label}] is on but checks nothing: set commands, or agent = true")
            if step.get("after") == "deploy" and not (step.get("wait") or "").strip():
                out.append(f'[{label}].wait is missing: with after = "deploy" it is the command that '
                           "exits 0 once the merge commit is live")
    graph_problems = graph.problems(d)
    out += graph_problems

    # reviewers and surfaces
    for name, reviewer in d.get("reviewers", {}).items():
        if not NAME.fullmatch(name):
            out.append(f"[reviewers.{name}]: a reviewer name is lowercase letters, digits and hyphens")
        test_table(f"reviewers.{name}", reviewer, REVIEWER_KEYS)
        if name not in BUILTIN_REVIEWERS and isinstance(reviewer, dict) and not reviewer.get("focus"):
            out.append(f"[reviewers.{name}].focus is missing: say what this reviewer looks for")
    for name, surface in d.get("surfaces", {}).items():
        if not NAME.fullmatch(name) or name == "none":
            out.append(f'[surfaces.{name}]: a surface name is lowercase letters, digits and hyphens, not "none"')
        test_table(f"surfaces.{name}", surface, {"when": a_filled_string, "reviewers": a_string_list})
        if isinstance(surface, dict):
            for key in ("when", "reviewers"):
                if key not in surface:
                    out.append(f"[surfaces.{name}].{key} is missing")
            listed = surface.get("reviewers", [])
            for r in listed if isinstance(listed, list) else []:
                if r not in d.get("reviewers", {}):
                    out.append(f'[surfaces.{name}].reviewers has "{r}", but there is no [reviewers.{r}]')

    # models
    keys = None if graph_problems else set(cfg.agent_keys(every=True)) | {"default"}
    for tool, table in d.get("models", {}).items():
        if tool not in cfg.adapters:
            out.append(f"[models.{tool}] is not a tool. Known: {', '.join(cfg.adapters)}")
            continue
        allowed = cfg.adapters[tool]["efforts"]["allowed"]
        for key, entry in table.items() if isinstance(table, dict) else []:
            label = f"models.{tool}"
            if keys is not None and key not in keys:
                out.append(f"[{label}].{key} names no agent. Agents: {', '.join(sorted(keys))}")
            if not isinstance(entry, dict):
                out.append(f'[{label}].{key} must be a table like {{ model = "...", effort = "..." }}')
                continue
            for risk, variant in [(None, entry)] + [(r, entry[r]) for r in RISKS if r in entry]:
                where = f"[{label}].{key}" + (f".{risk}" if risk else "")
                if not isinstance(variant, dict):
                    out.append(f"{where} must be a table with model and effort")
                    continue
                for k, v in variant.items():
                    if k in RISKS and risk is None:
                        continue
                    if k not in ("model", "effort"):
                        out.append(f"{where}.{k} is not a setting. Known: model, effort"
                                   + ("" if risk else ", low, medium, high"))
                    elif not isinstance(v, str) or not v:
                        out.append(f"{where}.{k} must be a non-empty string")
                    elif k == "effort" and v not in allowed:
                        out.append(f'{where}.effort = "{v}" is not an effort level {tool} documents. '
                                   f"Known: {', '.join(allowed)}")
    return out


# ---- Showing ---------------------------------------------------------------


def show(cfg):
    """The merged config as TOML, each value followed by the layer it came from."""
    groups = {}
    for key_path, value in leaves(cfg.data):
        if value == {}:
            groups.setdefault(key_path, [])
            continue
        depth = min(2, len(key_path) - 1)
        groups.setdefault(key_path[:depth], []).append((key_path[depth:], key_path, value))
    lines = []
    for group, rows in groups.items():
        if group:
            lines += ["", "[" + ".".join(group) + "]"]
        width = max((len(_key(k) + " = " + _value(v)) for k, _, v in rows), default=0)
        for key, key_path, value in rows:
            text = f"{_key(key)} = {_value(value)}"
            lines.append(f"{text.ljust(width)}  # {cfg.sources.get(key_path, 'default')}")
    return "\n".join(lines).strip() + "\n"


def _key(parts):
    return ".".join(p if re.fullmatch(r"[A-Za-z0-9_-]+", p) else json.dumps(p) for p in parts)


def _value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def as_json(cfg):
    """The merged config as rows, for the dashboard."""
    return [{"key": ".".join(p), "value": v, "source": cfg.sources.get(p, "default")}
            for p, v in leaves(cfg.data)]
