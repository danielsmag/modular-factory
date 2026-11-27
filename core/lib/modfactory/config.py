"""factory.toml: load it over the defaults, validate it, and say where each value came from."""

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import board, graph
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
    def live_reviewers(self):
        """The reviewers that are switched on."""
        return [name for name, r in self.reviewers.items() if graph.enabled(r)]

    @property
    def live_surfaces(self):
        """The surfaces intake may name: the ones that are switched on."""
        return {name: s for name, s in self.data["surfaces"].items() if graph.enabled(s)}

    @property
    def tools(self):
        return self.data["tools"]["enabled"]

    @property
    def jobs_dir(self):
        return self.repo / ".factory" / "jobs"

    @property
    def base(self):
        return self.data["project"]["base_branch"]

    def job_branch(self, job_id: str, *, revert: bool = False, fix: int = 0, tag: str = "") -> str:
        """The branch for a job, for the draft pull request that reverts it, or for its follow-up `fix`.

        `tag` is what the tracker puts before the job id, such as a Jira key. The pattern is checked
        here, when the name is chosen, before any branch exists.
        """
        prefix: str = str(self.data["project"]["branch_prefix"])
        named: str = tag + job_id
        suffix: str = f"revert-{named}" if revert else f"{named}-fix-{fix}" if fix else named
        name: str = prefix + suffix
        pattern: str = str(self.data["project"].get("branch_pattern", "")).strip()
        if pattern and re.fullmatch(pattern, name) is None:
            raise Refused(
                f"branch {name!r} does not match [project].branch_pattern {pattern!r}. "
                "Change [project].branch_prefix or [project].branch_pattern in factory.toml.")
        return name

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
        reviewers = self.reviewers if every else self.live_reviewers
        return keys + [f"reviewer-{name}" for name in reviewers]

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
        """Reviewers no surface claims run on every job; a surface adds its own.

        A reviewer on a switched-off surface stays claimed, so it does not start
        running on every job. Switched-off reviewers and surfaces add nothing.
        """
        claimed = {r for s in self.data["surfaces"].values() for r in s.get("reviewers", [])}
        live = self.live_reviewers
        chosen = [r for r in live if r not in claimed]
        for surface in surfaces:
            if surface not in self.live_surfaces:
                continue
            chosen += [r for r in self.data["surfaces"][surface]["reviewers"] if r in live and r not in chosen]
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


TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_*-]*")


def a_tool_list(value: object) -> str | None:
    """Claude Code tool names for an agent. `all` alone drops the allowlist; `default` is the built-in list."""
    if not isinstance(value, list) or not all(isinstance(i, str) and TOOL_NAME.fullmatch(i) for i in value):
        return 'must be a list of tool names, such as ["default", "WebFetch", "mcp__github__*"], or ["all"]'
    if "all" in value and len(value) > 1:
        return 'must be ["all"] on its own'
    return None


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


# A job id the prefix is checked against. Real ids are the same shape: NNN-slug.
_JOB_SAMPLE = "000-add-dark-mode"
# A Jira key and the hyphen after it, as a Jira job's branch carries them before the job id.
_KEY_SAMPLE = "PROJ-123-"
_FIX_SAMPLE = "000-add-dark-mode-fix-1"
# git check-ref-format: controls, space, DEL, and the characters it forbids anywhere.
_FORBIDDEN_IN_BRANCH = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


def branch_ref_problem(name: str) -> str | None:
    """Why `name` is not a legal git branch, or None when it is."""
    if not name or name == "@":
        return "it is empty or '@'"
    if name.startswith("-"):
        return "it starts with '-'"
    if name.startswith("/") or name.endswith("/"):
        return "it starts or ends with '/'"
    if "//" in name:
        return "it contains '//'"
    if ".." in name:
        return "it contains '..'"
    if "@{" in name:
        return "it contains '@{'"
    if name.endswith("."):
        return "it ends with '.'"
    forbidden: re.Match[str] | None = _FORBIDDEN_IN_BRANCH.search(name)
    if forbidden is not None:
        return f"it contains {forbidden.group()!r}"
    for component in name.split("/"):
        if component.startswith("."):
            return "a component starts with '.'"
        if component.endswith(".lock"):
            return "a component ends with '.lock'"
    return None


def a_branch_prefix(value: object) -> str | None:
    """A prefix that, with a job id appended, is a legal git branch. A trailing slash is the separator."""
    if not isinstance(value, str) or not value.strip():
        return "must be a non-empty string"
    problem: str | None = branch_ref_problem(value + _JOB_SAMPLE)
    if problem:
        return f"must be a legal git branch prefix ({problem})"
    return None


def _branch_pattern_gaps(project: object, steps: object = None, tag: str = "") -> list[str]:
    """Samples a set pattern must accept: one job branch, one revert branch and, with post-debug, one fix branch.

    `tag` is what the tracker puts before the job id in a branch name, such as a Jira key and a hyphen.
    """
    if not isinstance(project, dict):
        return []
    prefix: object = project.get("branch_prefix")
    pattern: object = project.get("branch_pattern")
    if not isinstance(prefix, str) or not isinstance(pattern, str):
        return []
    if a_branch_prefix(prefix) or a_regex(pattern):
        return []
    source: str = pattern.strip()
    if not source:
        return []
    gaps: list[str] = []
    keyed: str = " A Jira job's branch has the issue key before the job id." if tag else ""
    for sample in (prefix + tag + _JOB_SAMPLE, prefix + "revert-" + tag + _JOB_SAMPLE):
        if re.fullmatch(source, sample) is None:
            gaps.append(
                f"[project].branch_pattern does not match {sample!r}. "
                f"A job branch is the prefix plus the job id, and a revert branch inserts revert-.{keyed}")
    follow_ups: bool = isinstance(steps, dict) and any(
        isinstance(step, dict) and step.get("type") == "agent" and graph.enabled(step)
        and graph.role(name, step) == "post-debug" for name, step in steps.items())
    fix_sample: str = prefix + tag + _FIX_SAMPLE
    if follow_ups and re.fullmatch(source, fix_sample) is None:
        gaps.append(
            f"[project].branch_pattern does not match {fix_sample!r}. "
            "A post-debug step opens follow-up branches: the job branch plus -fix-N.")
    return gaps


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


def a_login_list(value: object) -> str | None:
    """Logins for the queue filter. `@me` is the operator. Empty means no filter."""
    if isinstance(value, list) and all(isinstance(item, str) and (item == "@me" or LOGIN.fullmatch(item))
                                       for item in value):
        return None
    return 'must be a list of GitHub logins or "@me"'


TRACKER_KINDS = ("github", "jira")
KIND_RULE = 'must be "github", "jira", or a list of them, such as ["github", "jira"]'


def tracker_kinds(raw: object) -> list[str]:
    """The trackers `[tracker].kind` names, in order: one name, or a list of distinct names. Empty for junk."""
    names: list[object]
    if isinstance(raw, str):
        names = [raw]
    elif isinstance(raw, list):
        names = list(raw)
    else:
        return []
    kinds: list[str] = [name for name in names if isinstance(name, str) and name in TRACKER_KINDS]
    if not kinds or len(kinds) != len(names) or len(set(kinds)) != len(kinds):
        return []
    return kinds


def a_tracker_kind(value: object) -> str | None:
    return None if tracker_kinds(value) else KIND_RULE


ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A Jira site: https, or plain http for a server on this machine.
SITE = re.compile(r"https://[A-Za-z0-9.-]+(?::\d+)?/?|http://(?:127\.0\.0\.1|localhost)(?::\d+)?/?")


def a_site(value: object) -> str | None:
    """Empty, or the address of a Jira site with no path."""
    if isinstance(value, str) and (not value or SITE.fullmatch(value)):
        return None
    return "must be an https:// address, such as https://acme.atlassian.net"


def an_env_name(value: object) -> str | None:
    """The name of an environment variable. The value it holds never goes in factory.toml."""
    if isinstance(value, str) and ENV_NAME.fullmatch(value):
        return None
    return "must be the name of an environment variable"


PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]+")


def a_project_key(value: object) -> str | None:
    """Empty, or a Jira project key."""
    if isinstance(value, str) and (not value or PROJECT_KEY.fullmatch(value)):
        return None
    return "must be a Jira project key, such as PROJ"


def a_jql(value: object) -> str | None:
    """A JQL filter. It is ANDed with the queue's own clause, so it cannot carry an order."""
    if isinstance(value, str) and not re.search(r"(?i)\border\s+by\b", value):
        return None
    return "must be a filter without ORDER BY"


# An account email. Empty means read email_env. The address is not a secret.
EMAIL_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def an_email(value: object) -> str | None:
    """Empty, or an account email. Empty means read email_env."""
    if isinstance(value, str) and (not value.strip() or EMAIL_ADDRESS.fullmatch(value.strip())):
        return None
    return "must be an email address, or empty to read email_env"


def a_token_file(value: object) -> str | None:
    """Empty, or a path to a file that holds the API token. The token itself never goes in factory.toml."""
    if isinstance(value, str) and "\n" not in value and "\r" not in value and "\x00" not in value:
        return None
    return "must be a path, or empty"


JIRA_KEYS = {"site": a_site, "board": an_int(0), "jql": a_jql, "email": an_email, "email_env": an_env_name,
             "token_env": an_env_name, "token_file": a_token_file, "issue_type": a_filled_string,
             "maintainers": a_string_list, "via": one_of("auto", "rest", "acli"), "project": a_project_key,
             "columns": lambda value: None}
# A Jira account id: letters, digits, hyphens and a colon, such as 712020:8a7d2c6e-1f6b-4c8e-9d1a-2b3c4d5e6f70.
ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}")


def a_jira_who(value: object) -> str | None:
    """Who may reply in Jira: a role, or a non-empty list of account ids."""
    if isinstance(value, str) and value in WHO_WORDS:
        return None
    if isinstance(value, list) and value and all(isinstance(item, str) and ACCOUNT.fullmatch(item) for item in value):
        return None
    return 'must be "author", "maintainer", "operator", or a list of Jira account ids'


def a_jira_account_list(value: object) -> str | None:
    """Jira account ids for the queue filter. `@me` is the operator. Empty means no filter."""
    if isinstance(value, list) and all(isinstance(item, str) and (item == "@me" or ACCOUNT.fullmatch(item))
                                       for item in value):
        return None
    return 'must be a list of Jira account ids or "@me"'


# With two trackers, people are named per tracker: a GitHub login and a Jira account id can look the same.
PEOPLE_CHECKS = {"github": a_login_list, "jira": a_jira_account_list}
WHO_CHECKS = {"github": a_who, "jira": a_jira_who}


def people_for(value: object, kind: str) -> list[str]:
    """The people a list, or a table of lists per tracker, names for one tracker. Empty when it names none."""
    if isinstance(value, dict):
        value = value.get(kind)
    return [str(item) for item in value] if isinstance(value, list) else []


def who_for(step: dict[str, Any], kind: str) -> str | list[str]:
    """Who a human step waits for on one tracker: a role, or the people named for that tracker."""
    who: object = step.get("who", "maintainer")
    if isinstance(who, dict):
        who = who.get(kind, "maintainer")
    if isinstance(who, list):
        return [str(item) for item in who]
    return who if isinstance(who, str) else "maintainer"


def _per_tracker_problems(label: str, key: str, value: object, kinds: list[str],
                          checks: dict[str, Callable[[object], str | None]], every: bool) -> list[str]:
    """A people list as a table per tracker: `key = { github = [...], jira = [...] }`.

    `every` asks for an entry per tracker, for a step that must know whom to ask on each.
    """
    example: str = f"{key} = {{ {', '.join(f'{kind} = [...]' for kind in kinds)} }}"
    if isinstance(value, str) and value in WHO_WORDS and every:
        return []
    if value == [] and not every:
        return []  # nobody named, as in the defaults: no filter on either tracker
    if not isinstance(value, dict):
        return [f"[{label}].{key} must name people per tracker when the project has two: {example} "
                f"(got {json.dumps(value)})"]
    out: list[str] = []
    for name, entry in value.items():
        if name not in kinds:
            out.append(f"[{label}].{key}.{name} is not a tracker of this project. Trackers: {', '.join(kinds)}")
        elif (problem := checks[name](entry)):
            out.append(f"[{label}].{key}.{name} {problem} (got {json.dumps(entry)})")
    missing: list[str] = [kind for kind in kinds if kind not in value]
    if every and missing:
        out.append(f"[{label}].{key} must name every tracker: {', '.join(missing)} is missing ({example})")
    return out


TABLES = {
    "project": {"description": a_string, "base_branch": a_filled_string,
                "merge_method": one_of("merge", "squash", "rebase"),
                "pr_title": a_string, "pr_title_pattern": a_regex, "issue_link": one_of("refs", "closes"),
                "branch_prefix": a_branch_prefix, "branch_pattern": a_regex},
    "run": {"concurrency": an_int(1), "start": a_filled_string, "escalate_to": a_filled_string,
            "pick": one_of("all", "mine"), "report_problems": a_bool,
            "authors": a_login_list, "assignees": a_login_list},
    "workspace": {"concurrency": an_int(1)},
    "checks": {"commands": a_string_list, "timeout_minutes": an_int(1)},
    "labels": {"queue": a_filled_string, "needs_info": a_filled_string, "needs_human": a_filled_string},
    "paths": {"worktrees": a_filled_string, "protected": a_string_list},
    "ports": {"base": an_int(1024), "step": an_int(3)},
    "skills": {"all": a_string_list},
    "rules": {"all": a_string_list},
}
TOOL_KEYS = {"claude": {"stop_hook": a_bool, "cost": a_bool, "plan_usage": a_bool, "resume_builder": a_bool,
                        "agent_tools": a_tool_list},
             "codex": {"intake_shell": a_bool}}
STEP_COMMON = {"type": one_of(*graph.TYPES), "enabled": a_bool, "skip": a_filled_string}
STEP_KEYS = {
    "agent": {"role": one_of(*graph.ROLE_OUTCOMES, "custom"), "instructions": a_filled_string,
              "instructions_file": a_filled_string, "writes": a_filled_string, "skills": a_string_list,
              "rules": a_string_list, "tools": a_tool_list, "context": a_string, "port": a_bool},
    "checks": {},
    "review": {},
    "human": {"who": a_who, "shows": a_string, "ask": a_string, "pr_ready": a_bool,
              "replies": one_of("any", "tool")},
    "merge": {},
    "verify": {"after": one_of("merge", "deploy"), "commands": a_string_list, "agent": a_bool,
               "retries": an_int(0), "on_failure": one_of("comment", "issue", "revert-pr"),
               "wait": a_string, "timeout_minutes": an_int(1), "url": a_string,
               "skills": a_string_list, "rules": a_string_list, "tools": a_tool_list, "context": a_string,
               "port": a_bool},
}
REVIEWER_KEYS = {"enabled": a_bool, "focus": a_filled_string, "prompt": a_filled_string, "context": a_string,
                 "skills": a_string_list, "rules": a_string_list, "tools": a_tool_list, "port": a_bool}
SURFACE_KEYS = {"enabled": a_bool, "when": a_filled_string, "reviewers": a_string_list}
BUILTIN_REVIEWERS = ("code", "security", "ux", "ui")
TOP = set(TABLES) | {"schema", "tools", "steps", "reviewers", "surfaces", "models", "tracker"}


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def prompt_path_problem(repo: Path, raw: str) -> str | None:
    """Why `raw` cannot be a prompt file, or None when it is a file inside `repo`."""
    parts: list[str] = raw.split("/")
    outside: bool = raw.startswith("/") or "\\" in raw or not parts or any(
        part in {"", ".", ".."} for part in parts)
    if outside:
        return f"must be a path inside the repository (got {json.dumps(raw)})"
    path: Path = repo / raw
    if not _path_inside(path, repo) or not path.is_file():
        return f'"{raw}" not found in the repository'
    return None


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
        if key == "fleet":
            out.append("[fleet] is now [workspace]. The cap across projects is set in workspace.toml, "
                       "or with --concurrency on `mfactory workspace queue`.")
        elif key not in TOP:
            out.append(f"[{key}] is not a section. Known: {', '.join(sorted(TOP))}")
    if d.get("schema") != SCHEMA:
        out.append(f"schema = {d.get('schema')!r}, but this factory reads schema {SCHEMA}. "
                   "Run `mfactory upgrade`, or set schema to match.")
    # People are named by GitHub login, or by Jira account id when Jira is the tracker. With both
    # trackers, each list is a table per tracker, checked below.
    kinds: list[str] = tracker_kinds(d["tracker"].get("kind")) if isinstance(d.get("tracker"), dict) else []
    uses_jira: bool = "jira" in kinds
    both: bool = len(kinds) > 1
    def skip(value: object) -> str | None:
        """Checked per tracker below."""
        return None

    run_people: dict[str, Callable[[object], str | None]] = (
        {"authors": skip, "assignees": skip} if both
        else {"authors": a_jira_account_list, "assignees": a_jira_account_list} if uses_jira else {})
    tables = TABLES | {"run": TABLES["run"] | run_people}
    human_keys = STEP_KEYS["human"] | ({"who": skip} if both else {"who": a_jira_who} if uses_jira else {})
    for name, spec in tables.items():
        test_table(name, d.get(name, {}), spec)
    run_table: object = d.get("run")
    if both and isinstance(run_table, dict):
        for key in ("authors", "assignees"):
            if key in run_table:
                out.extend(_per_tracker_problems("run", key, run_table[key], kinds, PEOPLE_CHECKS, every=False))
    if cfg.sources.get(("workspace", "concurrency")) == FILE:
        out.append("[workspace].concurrency is the cap across projects. Set it in workspace.toml, "
                   "or pass --concurrency to `mfactory workspace queue`.")
    for tag in ([""] if not uses_jira else [_KEY_SAMPLE] if not both else ["", _KEY_SAMPLE]):
        out.extend(_branch_pattern_gaps(d.get("project"), d.get("steps"), tag))

    # the tracker
    tracker = d.get("tracker", {})
    test_table("tracker", tracker, {"kind": a_tracker_kind, "jira": lambda value: None})
    jira = tracker.get("jira", {}) if isinstance(tracker, dict) else {}
    test_table("tracker.jira", jira, JIRA_KEYS)
    if isinstance(tracker, dict) and isinstance(jira, dict):
        valid_steps = {n: s for n, s in d.get("steps", {}).items()
                       if isinstance(s, dict) and s.get("type") in graph.TYPES}
        if "columns" in jira or uses_jira:
            out.extend(board.problems(valid_steps, jira.get("columns", {}), "tracker.jira.columns", uses_jira))
        if uses_jira:
            if not jira.get("site"):
                out.append("[tracker.jira].site is missing: the address of the Jira site, "
                           "such as https://acme.atlassian.net")
            if not jira.get("board"):
                out.append("[tracker.jira].board is missing: the id of the kanban board, "
                           "the number in its address")
            if not both and isinstance(d.get("project"), dict) and d["project"].get("issue_link") == "closes":
                out.append('[project].issue_link = "closes" needs GitHub issues: a pull request cannot '
                           'close a Jira issue. Use issue_link = "refs".')

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
        own_keys = human_keys if step["type"] == "human" else STEP_KEYS[step["type"]]
        spec = STEP_COMMON | own_keys | {"on": lambda v: None}
        test_table(label, step, spec)
        if both and step["type"] == "human" and "who" in step:
            out.extend(_per_tracker_problems(label, "who", step["who"], kinds, WHO_CHECKS, every=True))
        role_name: str = graph.role(name, step) if step["type"] == "agent" else ""
        if role_name == "custom" and not step.get("instructions") and not step.get("instructions_file"):
            out.append(f"[{label}].instructions is missing: say what this step's agent must do")
        if step["type"] == "agent" and "instructions_file" in step:
            if role_name != "custom":
                out.append(f"[{label}].instructions_file is only for a custom step")
            else:
                file_value: object = step.get("instructions_file")
                if isinstance(file_value, str) and file_value.strip():
                    file_problem: str | None = prompt_path_problem(cfg.repo, file_value)
                    if file_problem:
                        out.append(f"[{label}].instructions_file {file_problem}")
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
        if isinstance(reviewer, dict):
            needs_focus = name not in BUILTIN_REVIEWERS and reviewer.get("enabled", True) is not False
            if needs_focus and not reviewer.get("focus") and not reviewer.get("prompt"):
                out.append(f"[reviewers.{name}].focus is missing: say what this reviewer looks for")
            prompt_value: object = reviewer.get("prompt")
            if isinstance(prompt_value, str) and prompt_value.strip():
                prompt_problem: str | None = prompt_path_problem(cfg.repo, prompt_value)
                if prompt_problem:
                    out.append(f"[reviewers.{name}].prompt {prompt_problem}")
    for name, surface in d.get("surfaces", {}).items():
        if not NAME.fullmatch(name) or name == "none":
            out.append(f'[surfaces.{name}]: a surface name is lowercase letters, digits and hyphens, not "none"')
        test_table(f"surfaces.{name}", surface, SURFACE_KEYS)
        if isinstance(surface, dict):
            for key in ("when", "reviewers"):
                if key not in surface:
                    out.append(f"[surfaces.{name}].{key} is missing")
            listed = surface.get("reviewers", [])
            for r in listed if isinstance(listed, list) else []:
                known = d.get("reviewers", {})
                if r not in known:
                    out.append(f'[surfaces.{name}].reviewers has "{r}", but there is no [reviewers.{r}]')
                elif surface.get("enabled", True) is not False and isinstance(known[r], dict) \
                        and known[r].get("enabled", True) is False:
                    out.append(f'[surfaces.{name}].reviewers has "{r}", but [reviewers.{r}] is off. '
                               f'Set [surfaces.{name}] enabled = false, or drop "{r}" from its reviewers')
    reviewers_table = d.get("reviewers", {})
    if isinstance(reviewers_table, dict):
        any_live = any(not (isinstance(r, dict) and r.get("enabled", True) is False)
                       for r in reviewers_table.values())
        for step_name, step in steps.items():
            if isinstance(step, dict) and step.get("type") == "review" and graph.enabled(step) and not any_live:
                out.append(f"every reviewer is off, but [steps.{step_name}] is on. "
                           f"Switch it off with [steps.{step_name}] enabled = false")

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
