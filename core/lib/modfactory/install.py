"""`mfactory install`, `mfactory upgrade` and `mfactory doctor`."""

import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import checks, config, graph
from . import sync as syncer
from .util import CORE, SOURCE_REPO, ConfigError, FactoryError, Refused, run, tail, version

ITEMS = ["bin", "lib", "roles", "skill", "defaults.toml", "VERSION"]
LABELS = {
    "queue": ("ff4a26", "The software factory builds this issue"),
    "needs_info": ("d4a72c", "The factory is waiting for the issue's author"),
    "needs_human": ("b60205", "The factory is waiting for a maintainer"),
}

STARTER = """\
# The software factory's configuration. This is the only file you edit.
#
# It holds what this project changes; everything else is a default.
#   .factory/bin/mfactory config show   every setting and where its value comes from
#   .factory/bin/mfactory graph         the pipeline in effect
#   .factory/bin/mfactory models        the model and effort of each agent
# After a change, run `.factory/bin/mfactory sync` and commit the result.

schema = 1

[project]
description = ""            # what the project is for; intake uses it to decide what belongs
base_branch = "{base}"
# pr_title = "feat: add CSV export [ABC-123]"
# pr_title_pattern = '^(feat|fix|perf|refactor|chore|docs|test|ci|build|revert)(\\(.+\\))?!?: .+ \\[[A-Z]+-\\d+\\]$'
# Leave both unset to use the spec's title. With squash merge the title is the commit on the base branch.

[tools]
enabled = [{tools}]         # claude, cursor, codex

[run]
concurrency = 3             # jobs worked on at once

[checks]
commands = []               # for example ["npm ci", "npm run lint", "npm test"]

# ---- Merging ----------------------------------------------------------------
#
# By default a maintainer merges the pull request. The factory does not.
# Remove the leading "# " on all three tables to let the factory merge. Do that
# only when a merge to the base branch does not publish a release.

# [steps.approve]
# on = {{ APPROVE = "merge" }}

# [steps.ready-to-merge]
# enabled = false
# skip = "REJECT"

# [steps.merge]
# enabled = true

# ---- Optional steps: remove the leading "# " to switch one on --------------

# A maintainer approves the spec before anything is built.
# [steps.spec-approval]
# enabled = true

# A debugger diagnoses failing checks before the builder tries again.
# [steps.debug]
# enabled = true

# Check the change again after it is merged or deployed.
# [steps.verify]
# enabled = true
# after = "merge"           # or "deploy", with `wait` set to a command that exits 0 once it is live
# commands = []             # for example ["npm run test:smoke"]

# ---- Loop limits: how often a step may send a job back ---------------------

# [steps.gate]
# on = {{ FAIL = {{ max = 2 }} }}       # failed checks: retries before a maintainer is asked
# [steps.review]
# on = {{ CHANGES = {{ max = 2 }} }}    # review rounds that may ask for changes

# ---- Models: per tool, per step --------------------------------------------

# [models.claude]
# build = {{ model = "opus", effort = "high" }}
# reviewer-code = {{ model = "sonnet", effort = "medium", low = {{ model = "haiku", effort = "low" }} }}

# ---- Project skills --------------------------------------------------------

# [skills]
# all = ["code-style"]      # every agent reads these
# [steps.build]
# skills = ["testing"]
"""


def layout(root):
    """(core, adapters, web, licence folder) for a source checkout or an installed `.factory/`."""
    root = Path(root)
    if (root / "core" / "bin" / "mfactory").is_file() or (root / "core" / "bin" / "factory").is_file():
        return root / "core", root / "adapters", root / "web", root
    if (root / "bin" / "mfactory").is_file() or (root / "bin" / "factory").is_file():
        return root, root / "adapters", root / "web", root
    raise Refused(f"{root} is not a copy of the factory: it has no core/bin/mfactory")


def copy_core(source, repo):
    """Put the core into `<repo>/.factory/`, leaving `.factory/jobs/` alone."""
    core, adapters, web, licence = layout(source)
    dest = Path(repo) / ".factory"
    if core.resolve() == dest.resolve():
        return False
    dest.mkdir(exist_ok=True)
    skip = shutil.ignore_patterns("__pycache__", "*.pyc")
    sources = {item: core / item for item in ITEMS}
    sources |= {
        "adapters": adapters,
        "web": web,
        "LICENSE": licence / "LICENSE",
        "NOTICE": licence / "NOTICE",
    }
    for item, src in sources.items():
        if not src.exists():
            raise Refused(f"{source} is incomplete: {src} is missing")
        target = dest / item
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if src.is_dir():
            shutil.copytree(src, target, ignore=skip)
        else:
            shutil.copy2(src, target)
    # The page used to be copied as .factory/dashboard.html.
    stale: Path = dest / "dashboard.html"
    if stale.is_file():
        stale.unlink()
    for name in ("mfactory", "factory"):
        binary = dest / "bin" / name
        if binary.is_file():
            binary.chmod(0o755)
    return True


def base_branch(repo):
    res = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo, check=False)
    if res.returncode == 0 and "/" in res.stdout:
        return res.stdout.strip().split("/", 1)[1]
    res = run(["git", "branch", "--show-current"], cwd=repo, check=False)
    return res.stdout.strip() or "main"


def ignore_entries(repo, worktrees):
    """Add the runtime folders to .gitignore. Returns the lines added."""
    path = Path(repo) / ".gitignore"
    have = {line.strip() for line in path.read_text().splitlines()} if path.exists() else set()
    wanted = ["/.factory/jobs/", f"/{worktrees.strip('/')}/"]
    missing = [w for w in wanted if not _pattern_present(have, w)]
    if missing:
        text = path.read_text() if path.exists() else ""
        text += ("" if text.endswith("\n") or not text else "\n") + "\n# software factory runtime state\n"
        path.write_text(text + "\n".join(missing) + "\n")
    return missing


_TOOL_ORDER: tuple[str, ...] = ("claude", "cursor", "codex")
_EXCLUDE_NOTE: str = "# software factory, this checkout only"


def _pattern_present(have: set[str], pattern: str) -> bool:
    """True when `pattern` or the same path without slashes is already listed."""
    return pattern in have or pattern.lstrip("/") in have or pattern.strip("/") in have


def personal_patterns(cfg: config.Config) -> list[str]:
    """Ignore lines for a personal install: the factory, its runtime dirs, and generated files."""
    worktrees: str = str(cfg["paths"]["worktrees"]).strip("/")
    patterns: list[str] = [f"/{config.FILE}", "/.factory/", f"/{worktrees}/"]
    seen: set[str] = set(patterns)
    names: list[str] = [name for name in _TOOL_ORDER if name in cfg.adapters]
    names.extend(sorted(name for name in cfg.adapters if name not in _TOOL_ORDER))
    for name in names:
        paths: dict[str, object] = cfg.adapters[name]["paths"]
        agents: str = str(paths["agents"]).strip("/")
        skill: str = str(paths["skill"]).strip("/")
        for pattern in (f"/{agents}/mfactory-*", f"/{agents}/factory-*", f"/{skill}/"):
            if pattern not in seen:
                seen.add(pattern)
                patterns.append(pattern)
    return patterns


def git_path(repo: Path, name: str) -> Path:
    """A path inside this checkout's git dir, absolute."""
    res = run(["git", "rev-parse", "--git-path", name], cwd=repo)
    raw: str = res.stdout.strip()
    path: Path = Path(raw)
    if not path.is_absolute():
        path = (Path(repo) / path).resolve()
    return path


def excluded_by_info(repo: Path, path: str) -> bool:
    """True when `.git/info/exclude` is the rule that ignores `path`."""
    res = run(["git", "check-ignore", "-v", "--", path], cwd=repo, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return False
    source: str = res.stdout.split(":", 1)[0]
    return source.endswith("info/exclude")


def is_personal(repo: Path) -> bool:
    """True when this checkout already hides factory.toml in `.git/info/exclude`."""
    return excluded_by_info(Path(repo), config.FILE)


def exclude_entries(repo: Path, patterns: list[str]) -> list[str]:
    """Append factory patterns to `.git/info/exclude`. Returns the lines added."""
    path: Path = git_path(repo, "info/exclude")
    path.parent.mkdir(parents=True, exist_ok=True)
    text: str = path.read_text() if path.exists() else ""
    have: set[str] = {line.strip() for line in text.splitlines()}
    missing: list[str] = [pattern for pattern in patterns if not _pattern_present(have, pattern)]
    if missing:
        text += ("" if text.endswith("\n") or not text else "\n") + f"\n{_EXCLUDE_NOTE}\n"
        path.write_text(text + "\n".join(missing) + "\n")
    return missing


def create_labels(cfg, say):
    for key, (colour, description) in LABELS.items():
        name = cfg["labels"][key]
        try:
            res = run(["gh", "label", "create", name, "--color", colour, "--description", description,
                       "--force"], cwd=cfg.repo, check=False)
        except FileNotFoundError:
            say("warn  the GitHub CLI `gh` isn't installed, so the labels were not created")
            return
        if res.returncode != 0:
            why = (res.stderr or res.stdout).strip().splitlines()
            say(f"warn  label `{name}` was not created: {why[0] if why else 'gh failed'}")
            say("      Create the labels later with `.factory/bin/mfactory install`.")
            return
    say("ok    GitHub labels: " + ", ".join(cfg["labels"][k] for k in LABELS))


def init(repo: Path, tools: list[str] | None = None, say: Callable[[str], None] = print,
         *, personal: bool = False) -> None:
    """Install the factory into `repo`, or repair an install. Never overwrites factory.toml.

    `personal` writes ignore lines to `.git/info/exclude` and leaves `.gitignore` alone.
    A later install stays personal when that exclude file already hides factory.toml.
    """
    repo = Path(repo)
    copied = copy_core(CORE.parent if (CORE.parent / "core").is_dir() else CORE, repo)
    say(f"ok    .factory/ {'installed' if copied else 'already in place'} (version {version()})")

    path = repo / config.FILE
    known = config.load_adapters()
    for tool in tools or []:
        if tool not in known:
            raise Refused(f'"{tool}" is not a tool. Known: {", ".join(known)}')
    if path.exists():
        say(f"ok    {config.FILE} exists and was left as it is")
        if tools:
            say(f"      To change the tools, set [tools].enabled in {config.FILE}.")
    else:
        chosen = ", ".join(json.dumps(t) for t in (tools or ["claude"]))
        path.write_text(STARTER.format(base=base_branch(repo), tools=chosen))
        say(f"ok    {config.FILE} written")

    cfg = config.load(repo)
    personal = personal or is_personal(repo)
    if personal:
        added = exclude_entries(repo, personal_patterns(cfg))
        say("ok    .git/info/exclude " + (
            f"gained {', '.join(added)}" if added else "already ignores the factory (personal install)"))
    else:
        added = ignore_entries(repo, cfg["paths"]["worktrees"])
        say("ok    .gitignore " + (f"gained {', '.join(added)}" if added else "already ignores the runtime folders"))
    actions, notes = syncer.sync(cfg)
    say(f"ok    generated files for {', '.join(cfg.tools)}: "
        + (f"{len(actions)} written" if actions else "up to date"))
    for note in notes:
        say(f"note  {note}")
    create_labels(cfg, say)

    skills = syncer.found_skills(repo)
    if skills:
        say("")
        say("Project skills found. List the ones agents should read under [skills] or a step's `skills`:")
        for name, where, description in skills:
            say(f"  {name}  ({where})" + (f"  {description[:70]}" if description else ""))
    say("")
    say("Next:")
    say(f"  1. In your tool, run `/mfactory setup` to fill in {config.FILE} (or edit it yourself).")
    say("  2. Run `.factory/bin/mfactory doctor`.")
    if personal:
        say(f"  3. Leave {config.FILE}, .factory/ and the generated files untracked. This checkout ignores them.")
    else:
        say(f"  3. Commit {config.FILE}, .factory/ and the generated files to {cfg.base}, and push.")
    say(f"  4. Label an issue `{cfg['labels']['queue']}` and run `/mfactory run`.")


# ---- upgrade ---------------------------------------------------------------


def fetch_release(ref, into):
    """Download and unpack one version of the factory. Returns the folder it unpacked to."""
    if ref == "latest":
        try:
            with urllib.request.urlopen(f"https://api.github.com/repos/{SOURCE_REPO}/releases/latest",
                                        timeout=30) as res:
                ref = json.load(res)["tag_name"]
        except (urllib.error.URLError, KeyError, ValueError):
            ref = "master"  # no release published yet
    kind = "heads" if ref == "master" else "tags"
    url = f"https://codeload.github.com/{SOURCE_REPO}/tar.gz/refs/{kind}/{ref}"
    archive = Path(into) / "factory.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=60) as res, archive.open("wb") as out:
            shutil.copyfileobj(res, out)
    except urllib.error.URLError as e:
        raise Refused(f"could not download {url}: {e}") from e
    with tarfile.open(archive) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(into, filter="data")
        else:  # Python 3.11.0 to 3.11.3
            tar.extractall(into)  # noqa: S202
    folders = [p for p in Path(into).iterdir() if p.is_dir()]
    if len(folders) != 1:
        raise Refused(f"{url} did not unpack to one folder")
    return folders[0]


def project_cli(repo: Path) -> Path:
    """The factory command inside a project. Prefers `mfactory`, then the 0.1.0 name."""
    current: Path = repo / ".factory" / "bin" / "mfactory"
    if current.is_file():
        return current
    return repo / ".factory" / "bin" / "factory"


def upgrade(repo, ref="latest", source=None, say=print):
    """Replace `.factory/` with another version and re-sync. factory.toml is not touched."""
    repo = Path(repo)
    if not project_cli(repo).exists():
        raise Refused("the factory isn't installed here. Run `mfactory install` first.")
    before = (repo / ".factory" / "VERSION").read_text().strip()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(source).resolve() if source else fetch_release(ref, tmp)
        core, _, _, _ = layout(root)
        after = (core / "VERSION").read_text().strip()
        new_schema = config.read_toml(core / "defaults.toml")["schema"]
        have = config.read_toml(repo / config.FILE).get("schema") if (repo / config.FILE).exists() else new_schema
        if new_schema != have:
            raise Refused(f"version {after} reads schema {new_schema}, and {config.FILE} is schema {have}. "
                          f"Follow the upgrade notes in CHANGELOG.md for {after}, set schema = {new_schema}, "
                          "then upgrade again.")
        copy_core(root, repo)
    say(f"ok    .factory/ {before} -> {after}")
    # The new version's own code must do the sync, not the one still loaded here.
    res = subprocess.run([sys.executable, str(project_cli(repo)), "sync"], cwd=repo)
    if res.returncode != 0:
        raise Refused(f"the new version could not sync. Fix {config.FILE} as it says, then run "
                      "`.factory/bin/mfactory sync`.")
    if is_personal(repo):
        say("Next: run `.factory/bin/mfactory doctor`. "
            "Leave factory.toml, .factory/ and the generated files untracked.")
    else:
        say(f"Next: run `.factory/bin/mfactory doctor`, then commit .factory/ and the generated files to "
            f"{base_branch(repo)}.")


# ---- doctor ----------------------------------------------------------------


def _version(command):
    try:
        res = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = (res.stdout or res.stderr).strip().splitlines()
    return out[0] if out else ""


def doctor(repo, run_checks=True, say=print):
    """Check everything a run depends on. Returns the number of failures."""
    repo = Path(repo)
    failures = 0

    def ok(text):
        say(f"ok    {text}")

    def warn(text):
        say(f"warn  {text}")

    def fail(text):
        nonlocal failures
        failures += 1
        say(f"FAIL  {text}")

    ok(f"factory {version()}, Python {sys.version.split()[0]}")
    for command, why in (("git", "every job is a branch and a worktree"), ("gh", "issues, comments and merges")):
        found = _version(command)
        (ok if found is not None else fail)(f"{command}: {found}" if found is not None
                                            else f"`{command}` isn't installed ({why})")
    if _version("gh") is not None:
        res = run(["gh", "auth", "status"], cwd=repo, check=False)
        (ok if res.returncode == 0 else fail)("gh is signed in" if res.returncode == 0
                                              else "gh isn't signed in: run `gh auth login`")

    # config and graph
    try:
        cfg = config.load(repo)
    except ConfigError as e:
        for problem in e.problems:
            fail(problem)
        say(f"\n{failures} problem(s). Fix {config.FILE} first; the other checks need a valid config.")
        return failures
    except FactoryError as e:
        fail(str(e))
        return failures
    off = [n for n, s in cfg.steps.items() if not graph.enabled(s)]
    ok(f"{config.FILE} is valid: {len(cfg.steps) - len(off)} steps on"
       + (f", off: {', '.join(off)}" if off else ""))
    gap: str | None = graph.hand_merge_warning(cfg.steps)
    if gap:
        warn(gap)

    # tools
    for tool in cfg.tools:
        adapter = cfg.adapters[tool]
        found = _version(adapter["cli"])
        if found is None:
            warn(f"{adapter['label']}: `{adapter['cli']}` isn't on PATH. That is fine if you run it as an app.")
        else:
            ok(f"{adapter['label']}: {found}")

    # skills and generated files
    try:
        actions, notes = syncer.sync(cfg, check=True)
    except ConfigError as e:
        for problem in e.problems:
            fail(problem)
        actions, notes = None, []
    if actions:
        for action, rel in actions:
            what = {"create": "is missing", "remove": "is left over from an earlier config",
                    "update": "differs from what factory.toml generates (edited by hand, or not synced)"}[action]
            fail(f"{rel} {what}")
        say("      Run `.factory/bin/mfactory sync`. It overwrites hand edits: put changes in factory.toml.")
    elif actions is not None:
        ok("generated agent and skill files match factory.toml")
    for note in notes:
        say(f"note  {note}")

    # the installed core
    installed = repo / ".factory"
    if not project_cli(repo).exists():
        fail(".factory/ isn't installed: run `mfactory install`")
    elif CORE.resolve() != installed.resolve():
        warn(f"this command runs from {CORE}, not from {installed}")
    missing = [i for i in ITEMS + ["adapters", "web"] if not (installed / i).exists()]
    if missing and project_cli(repo).exists():
        fail(f".factory/ is incomplete, missing: {', '.join(missing)}. Run `.factory/bin/mfactory upgrade`.")

    # git
    base = cfg.base
    if run(["git", "remote", "get-url", "origin"], cwd=repo, check=False).returncode != 0:
        fail("git has no `origin` remote; jobs branch from and push to origin")
    elif run(["git", "fetch", "origin", base], cwd=repo, check=False).returncode != 0:
        fail(f'origin has no branch "{base}" ([project].base_branch), or it can\'t be fetched')
    else:
        ok(f"origin/{base} can be fetched")
        tracked = set(run(["git", "ls-tree", "-r", "--name-only", f"origin/{base}", "--", config.FILE,
                           ".factory/bin/mfactory", ".factory/bin/factory"], cwd=repo, check=False).stdout.split())
        if config.FILE not in tracked or not tracked & {".factory/bin/mfactory", ".factory/bin/factory"}:
            if excluded_by_info(repo, config.FILE) and excluded_by_info(repo, ".factory"):
                ok(f"{config.FILE} and .factory/ are ignored in this checkout (.git/info/exclude). "
                   f"Worktrees built from origin/{base} do not contain them. "
                   "A check that needs those files must use the main checkout.")
            else:
                warn(f"{config.FILE} and .factory/ aren't on origin/{base} yet. Commit and push them: "
                     "jobs build from that branch.")
    ignored = run(["git", "check-ignore", "-q", ".factory/jobs/x"], cwd=repo, check=False).returncode == 0
    (ok if ignored else fail)(".factory/jobs/ is ignored by git" if ignored
                              else ".factory/jobs/ isn't ignored by git: run `mfactory install`")

    # labels
    if _version("gh") is not None:
        res = run(["gh", "label", "list", "--limit", "300", "--json", "name"], cwd=repo, check=False)
        if res.returncode == 0:
            have = {label["name"] for label in json.loads(res.stdout)}
            gone = [n for n in cfg["labels"].values() if n not in have]
            (fail if gone else ok)(f"GitHub labels missing: {', '.join(gone)}. Run `mfactory install`." if gone
                                   else "GitHub labels exist")
        else:
            warn("could not list the repository's labels")

    # checks on the base branch
    commands = cfg["checks"]["commands"]
    if not commands:
        fail("[checks].commands is empty, so nothing proves a build works. Run `/mfactory setup`.")
    elif not run_checks:
        warn("the checks were not run (--skip-checks)")
    elif failures:
        warn("the checks were not run, because of the failures above")
    else:
        say(f"...   running the checks on origin/{base}")
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "base"
            added = run(["git", "worktree", "add", "--detach", str(tree), f"origin/{base}"], cwd=repo, check=False)
            if added.returncode != 0:
                fail(f"could not check out origin/{base}: {added.stderr.strip()}")
            else:
                try:
                    code, log, seconds = checks.run_commands(
                        commands, tree, checks.command_env(cfg, "doctor"), cfg["checks"]["timeout_minutes"])
                finally:
                    run(["git", "worktree", "remove", "--force", str(tree)], cwd=repo, check=False)
                    run(["git", "worktree", "prune"], cwd=repo, check=False)
                if code == 0:
                    ok(f"the checks pass on origin/{base} ({seconds}s)")
                else:
                    fail(f"the checks fail on origin/{base} (exit {code}), so every job would fail:")
                    say("\n".join("      " + line for line in tail(log, 15).splitlines()))

    say("")
    say("Everything a run needs is in place." if not failures else f"{failures} problem(s) to fix.")
    return failures
