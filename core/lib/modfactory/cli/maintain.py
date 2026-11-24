"""Install, upgrade, uninstall and status. The commands a person runs; agents do not."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import config
from .. import sync as syncer
from .. import workspace as workspace_mod
from ..install import fetch_release, is_personal, layout, project_cli, upgrade
from ..util import (
    CORE,
    SOURCE_REPO,
    ConfigError,
    FactoryError,
    Refused,
    find_repo,
    is_generated,
    run,
    user_dir,
    version,
)


def global_home() -> Path | None:
    """The directory `install.sh --global` wrote, when this process is that copy."""
    home: Path = CORE.parent
    if (home / ".global").is_file() and (home / "core" / "bin" / "mfactory").is_file():
        return home
    return None


def global_install() -> Path | None:
    """The global CLI tree: this process when it is that copy, otherwise the user directory."""
    here: Path | None = global_home()
    if here is not None:
        return here
    home: Path = user_dir()
    if (home / ".global").is_file() and (home / "core" / "bin" / "mfactory").is_file():
        return home
    return None


def latest_release() -> str | None:
    """The latest published version, without a leading v. None when GitHub can't be reached."""
    url: str = f"https://api.github.com/repos/{SOURCE_REPO}/releases/latest"
    try:
        with urllib.request.urlopen(url, timeout=10) as res:
            tag: object = json.load(res)["tag_name"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(tag, str) or not tag:
        return None
    return tag[1:] if tag.startswith("v") else tag


def status(repo: Path, say: Callable[[str], None] = print) -> int:
    """One project's version, tools, and whether its generated files match factory.toml."""
    say(f"cli     {version()}  {CORE}")
    installed: Path = Path(repo) / ".factory" / "VERSION"
    if not installed.is_file():
        say("project not installed. Run `mfactory install`.")
        return 1
    project_version: str = installed.read_text().strip()
    say(f"project {project_version}")
    try:
        cfg = config.load(repo)
    except ConfigError as error:
        for problem in error.problems:
            say(f"config  {problem}")
        return 1
    except FactoryError as error:
        say(f"config  {error}")
        return 1
    say(f"tools   {', '.join(cfg.tools)}")
    try:
        actions, _notes = syncer.sync(cfg, check=True)
    except ConfigError as error:
        say(f"sync    {error.problems[0]}")
    else:
        if actions:
            say(f"sync    {len(actions)} file(s) out of date. Run `mfactory sync`.")
        else:
            say("sync    up to date")
    latest: str | None = latest_release()
    if latest is None:
        say("latest  unknown (offline or no release yet)")
    elif latest != project_version:
        say(f"latest  {latest}  (upgrade available)")
    else:
        say(f"latest  {latest}")
    return 0


def project_summary(repo: Path) -> str:
    """A short status for one project in the workspace, using that project's own `sync --check`."""
    version_file: Path = Path(repo) / ".factory" / "VERSION"
    if not version_file.is_file():
        return "not installed"
    project_version: str = version_file.read_text().strip()
    binary: Path = project_cli(repo)
    if not binary.is_file():
        return f"{project_version}  mfactory missing"
    result: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, str(binary), "sync", "--check"], cwd=repo, capture_output=True, text=True,
    )
    changes: list[str] = [line for line in result.stdout.splitlines() if line.startswith("would ")]
    if result.returncode != 0 and not changes:
        detail: list[str] = (result.stderr or result.stdout).strip().splitlines()
        return f"{project_version}  sync failed" + (f": {detail[-1]}" if detail else "")
    if changes:
        return f"{project_version}  sync {len(changes)} out of date"
    return f"{project_version}  sync up to date"


def status_all(say: Callable[[str], None] = print) -> int:
    """One row per project in the workspace."""
    group: workspace_mod.Workspace = workspace_mod.Workspace.find()
    if not group.projects:
        say("no projects yet. Run `mfactory workspace add` inside a project.")
        return 0
    latest: str | None = latest_release()
    behind: bool = False
    for project in group.projects:
        summary: str = project_summary(project.path)
        installed: str = summary.split("  ", 1)[0]
        release: str
        if latest is None:
            release = "latest unknown"
        elif installed == "not" or not installed[:1].isdigit():
            release = f"latest {latest}"
        elif installed != latest:
            release = f"latest {latest} (upgrade available)"
            behind = True
        else:
            release = f"latest {latest}"
        say(f"{group.label(project)}  {summary}  {release}")
    if behind:
        say("run `mfactory upgrade`")
    return 0


def _unlink(path: Path, repo: Path, removed: list[str]) -> None:
    if not path.exists() and not path.is_symlink():
        return
    rel: str = str(path.relative_to(repo))
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    removed.append(rel)
    parent: Path = path.parent
    if parent != repo and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


def _generated_paths(repo: Path) -> list[Path]:
    """Agent and skill files sync owns. Falls back to the generated marker when config won't load."""
    try:
        cfg = config.load(repo)
    except FactoryError:
        cfg = None
    if cfg is not None:
        try:
            files, _notes = syncer.plan(cfg)
            return [repo / rel for rel in [*files, *syncer.stale(cfg, files)]]
        except ConfigError:
            pass
    found: list[Path] = []
    patterns: tuple[str, ...] = (
        ".claude/agents/mfactory-*", ".claude/agents/factory-*",
        ".cursor/agents/mfactory-*", ".cursor/agents/factory-*",
        ".codex/agents/mfactory-*", ".codex/agents/factory-*",
    )
    for pattern in patterns:
        for path in sorted(repo.glob(pattern)):
            if path.is_file() and is_generated(path.read_text(errors="replace")):
                found.append(path)
    skill_dirs: tuple[str, ...] = (
        ".claude/skills/mfactory", ".claude/skills/factory", ".agents/skills/mfactory",
        ".agents/skills/factory", ".cursor/skills/mfactory", ".cursor/skills/factory",
    )
    for rel in skill_dirs:
        path: Path = repo / rel / "SKILL.md"
        if path.is_file() and is_generated(path.read_text(errors="replace")):
            found.append(path)
    return found


def _remove_worktrees(repo: Path, folder: Path) -> None:
    listed: subprocess.CompletedProcess[str] = run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, check=False,
    )
    if listed.returncode == 0 and folder.exists():
        root: Path = folder.resolve()
        for block in listed.stdout.split("\n\n"):
            for line in block.splitlines():
                if not line.startswith("worktree "):
                    continue
                path: Path = Path(line.split(" ", 1)[1])
                try:
                    inside: bool = path.resolve().is_relative_to(root)
                except OSError:
                    inside = False
                if inside:
                    run(["git", "worktree", "remove", "--force", str(path)], cwd=repo, check=False)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    run(["git", "worktree", "prune"], cwd=repo, check=False)


def _forget(repo: Path, say: Callable[[str], None]) -> None:
    try:
        group: workspace_mod.Workspace = workspace_mod.Workspace.find(repo)
    except Refused:
        return
    try:
        group.remove(str(repo))
    except Refused:
        return
    say(f"ok    removed from {group.file}")


def uninstall(repo: Path, remove_config: bool = False, labels: bool = False,
              say: Callable[[str], None] = print) -> None:
    """Remove the factory from a repository. factory.toml stays unless remove_config is set."""
    repo = Path(repo)
    removed: list[str] = []
    for path in _generated_paths(repo):
        _unlink(path, repo, removed)
    factory_dir: Path = repo / ".factory"
    if factory_dir.exists():
        shutil.rmtree(factory_dir)
        removed.append(".factory/")
    worktrees: str = ".worktrees"
    try:
        cfg = config.load(repo)
    except FactoryError:
        cfg = None
    else:
        worktrees = cfg["paths"]["worktrees"]
    folder: Path = repo / worktrees
    had_worktrees: bool = folder.exists()
    _remove_worktrees(repo, folder)
    if had_worktrees:
        removed.append(worktrees.strip("/") + "/")
    if remove_config:
        config_path: Path = repo / config.FILE
        if config_path.is_file():
            config_path.unlink()
            removed.append(config.FILE)
    for rel in removed:
        say(f"remove  {rel}")
    if labels:
        if cfg is None:
            say("warn  labels were not deleted: factory.toml could not be read")
        else:
            for name in cfg["labels"].values():
                res = run(["gh", "label", "delete", name, "--yes"], cwd=repo, check=False)
                if res.returncode == 0:
                    say(f"ok    label {name} deleted")
                else:
                    why: list[str] = (res.stderr or res.stdout).strip().splitlines()
                    say(f"warn  label `{name}` was not deleted: {why[0] if why else 'gh failed'}")
    _forget(repo, say)


@dataclass(frozen=True)
class Target:
    """One CLI tree or checkout the upgrade plan knows about."""

    kind: Literal["global", "project"]
    label: str
    path: Path
    before: str
    after: str
    action: Literal["upgrade", "current", "schema", "fail"]
    personal: bool = False
    base: str = ""
    have: object = None
    need: object = None
    reason: str = ""


@contextmanager
def _release_root(ref: str, source: str | None) -> Iterator[Path]:
    """Yield one factory tree. A source path is used as given; otherwise the release is downloaded once."""
    if source is not None:
        yield Path(source).resolve()
        return
    temporary: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
    try:
        yield fetch_release(ref, temporary.name)
    finally:
        temporary.cleanup()


def _release_facts(root: Path) -> tuple[str, object]:
    """The version and schema of a factory tree."""
    located: tuple[Path, Path, Path, Path] = layout(root)
    core: Path = located[0]
    after: str = (core / "VERSION").read_text().strip()
    need: object = config.read_toml(core / "defaults.toml")["schema"]
    return after, need


def _global_target(home: Path, after: str) -> Target:
    version_file: Path = home / "core" / "VERSION"
    before: str = version_file.read_text().strip() if version_file.is_file() else ""
    action: Literal["upgrade", "current"] = "current" if before and before == after else "upgrade"
    return Target("global", "global", home, before, after, action)


def _project_base(repo: Path) -> str:
    try:
        loaded: config.Config = config.load(repo)
    except FactoryError:
        return ""
    return str(loaded.base)


def _project_target(repo: Path, label: str, after: str, need: object) -> Target:
    if not project_cli(repo).is_file():
        return Target(
            "project", label, repo, "", after, "fail",
            reason="the factory isn't installed here. Run `mfactory install` first.",
        )
    version_file: Path = repo / ".factory" / "VERSION"
    if not version_file.is_file():
        return Target("project", label, repo, "", after, "fail", reason=f"{version_file} is missing")
    before: str = version_file.read_text().strip()
    config_file: Path = repo / config.FILE
    try:
        have: object = need if not config_file.is_file() else config.read_toml(config_file).get("schema")
    except FactoryError as error:
        return Target("project", label, repo, before, after, "fail", reason=str(error))
    if have != need:
        return Target("project", label, repo, before, after, "schema", have=have, need=need)
    if before == after:
        return Target("project", label, repo, before, after, "current")
    return Target(
        "project", label, repo, before, after, "upgrade",
        personal=is_personal(repo), base=_project_base(repo),
    )


def _plan_detail(target: Target) -> str:
    shown: str = target.before or "unknown"
    if target.action == "upgrade":
        detail: str = f"{shown} -> {target.after}"
        if target.kind == "project":
            detail += "   personal" if target.personal else "   team"
        return detail
    if target.action == "current":
        return f"{target.after}            current"
    if target.action == "schema":
        return f"{shown}            schema {target.have}, release needs {target.need}"
    return f"FAIL  {target.reason}"


def _plan_rows(targets: list[Target]) -> list[str]:
    width: int = max(len(target.label) for target in targets)
    return [f"{target.label:<{width}}  {_plan_detail(target)}" for target in targets]


def _changelog_note(root: Path, version: str) -> str:
    """The changelog section for one version, at most 12 lines."""
    located: tuple[Path, Path, Path, Path] = layout(root)
    path: Path = located[3] / "CHANGELOG.md"
    if not path.is_file():
        return ""
    heading: str = f"## {version.lstrip('v')}"
    lines: list[str] = path.read_text().splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body[:12])


def _count_line(upgraded: int, current: int, skipped: int, failed: int) -> str:
    parts: list[str] = [f"{upgraded} upgraded", f"{current} current"]
    if skipped:
        parts.append(f"{skipped} skipped")
    if failed:
        parts.append(f"{failed} failed")
    return ", ".join(parts)


def _project_followup(target: Target) -> str:
    if target.personal:
        return (
            f"{target.label}  run `.factory/bin/mfactory doctor`. "
            "Leave factory.toml, .factory/ and the generated files untracked."
        )
    if target.base:
        return (
            f"{target.label}  commit .factory/ and the generated files, push origin/{target.base}, "
            "then `.factory/bin/mfactory doctor`"
        )
    return (
        f"{target.label}  commit .factory/ and the generated files, then push the base branch, "
        "then `.factory/bin/mfactory doctor`"
    )


def _replace_global(home: Path, root: Path, before: str, after: str, say: Callable[[str], None]) -> None:
    """Replace the global CLI tree with root, keeping workspace.toml."""
    located: tuple[Path, Path, Path, Path] = layout(root)
    core: Path = located[0]
    adapters: Path = located[1]
    web: Path = located[2]
    licence: Path = located[3]
    with tempfile.TemporaryDirectory() as tmp:
        tree: Path = Path(tmp) / "next"
        tree.mkdir()
        ignored: Callable[..., set[str]] = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(core, tree / "core", ignore=ignored)
        shutil.copytree(adapters, tree / "adapters", ignore=ignored)
        shutil.copytree(web, tree / "web", ignore=ignored)
        for name in ("LICENSE", "NOTICE"):
            src: Path = licence / name
            if src.is_file():
                shutil.copy2(src, tree / name)
        (tree / ".global").write_text(f"{home}\n")
        for name in ("workspace.toml", "fleet.toml"):
            workspace_list: Path = home / name
            if workspace_list.is_file():
                shutil.copy2(workspace_list, tree / "workspace.toml")
                break
        previous: Path = home.with_name(home.name + ".previous")
        if previous.exists():
            shutil.rmtree(previous)
        try:
            home.rename(previous)
        except OSError as error:
            raise Refused(f"could not replace {home}: {error}") from error
        try:
            shutil.copytree(tree, home)
        except Exception:
            if not home.exists():
                previous.rename(home)
            raise
        shutil.rmtree(previous, ignore_errors=True)
    shown: str = before or "unknown"
    say(f"ok    global install {shown} -> {after}  {home}")


def _apply(
    target: Target, root: Path, say: Callable[[str], None],
) -> Literal["upgraded", "current", "schema", "failed"]:
    if target.action == "current":
        say(f"skip  {target.label}  already {target.after}")
        return "current"
    if target.action == "schema":
        say(f"skip  {target.label}  schema {target.have}, release needs {target.need}")
        return "schema"
    if target.action == "fail":
        say(f"{target.label}  FAIL  {target.reason}")
        return "failed"

    def report(line: str, label: str = target.label) -> None:
        say(f"{label}  {line}")

    try:
        if target.kind == "global":
            _replace_global(target.path, root, target.before, target.after, say)
        else:
            upgrade(target.path, source=str(root), say=report)
    except FactoryError as error:
        say(f"{target.label}  FAIL  {error}")
        return "failed"
    return "upgraded"


def _run_upgrade(targets: list[Target], root: Path, dry_run: bool, say: Callable[[str], None]) -> int:
    """Print the plan, then upgrade every row that can move. A dry run stops after the plan."""
    if not targets:
        return 0
    say(targets[0].after)
    for row in _plan_rows(targets):
        say(row)
    if dry_run:
        return 0
    say("")
    upgraded: int = 0
    current: int = 0
    skipped: int = 0
    failed: int = 0
    upgraded_projects: list[Target] = []
    schema_targets: list[Target] = []
    for target in targets:
        outcome: Literal["upgraded", "current", "schema", "failed"] = _apply(target, root, say)
        if outcome == "upgraded":
            upgraded += 1
            if target.kind == "project":
                upgraded_projects.append(target)
        elif outcome == "current":
            current += 1
        elif outcome == "schema":
            skipped += 1
            schema_targets.append(target)
        else:
            failed += 1
    say("")
    say(_count_line(upgraded, current, skipped, failed))
    for target in upgraded_projects:
        say(_project_followup(target))
    noted: bool = False
    for target in schema_targets:
        say(f"{target.label}  set schema = {target.need} in {config.FILE}, then `mfactory upgrade`")
        if not noted:
            note: str = _changelog_note(root, target.after)
            if note:
                say(note)
            noted = True
    return 1 if failed or skipped else 0


def _installed_checkout() -> Path | None:
    """The checkout you are in, when the factory is installed there."""
    try:
        repo: Path = find_repo()
    except Refused:
        return None
    if project_cli(repo).is_file():
        return repo
    return None


def _workspace() -> workspace_mod.Workspace | None:
    """The workspace in effect. None when no project has been added yet."""
    try:
        return workspace_mod.Workspace.find()
    except Refused as error:
        if str(error).startswith("no workspace"):
            return None
        raise


def upgrade_self(
    ref: str = "latest",
    source: str | None = None,
    say: Callable[[str], None] = print,
    *,
    dry_run: bool = False,
) -> int:
    """Replace the global CLI tree from `install.sh --global`."""
    home: Path | None = global_install()
    if home is None:
        raise Refused("no global install. `install.sh --global` installs mfactory for your user.")
    with _release_root(ref, source) as root:
        facts: tuple[str, object] = _release_facts(root)
        after: str = facts[0]
        return _run_upgrade([_global_target(home, after)], root, dry_run, say)


def upgrade_all(
    ref: str = "latest",
    source: str | None = None,
    say: Callable[[str], None] = print,
    *,
    dry_run: bool = False,
) -> int:
    """Upgrade every project in the workspace. One failure does not stop the others."""
    group: workspace_mod.Workspace = workspace_mod.Workspace.find()
    if not group.projects:
        say("no projects yet. Run `mfactory workspace add` inside a project.")
        return 0
    with _release_root(ref, source) as root:
        facts: tuple[str, object] = _release_facts(root)
        after: str = facts[0]
        need: object = facts[1]
        targets: list[Target] = [
            _project_target(project.path, group.label(project), after, need) for project in group.projects
        ]
        return _run_upgrade(targets, root, dry_run, say)


def upgrade_everything(
    ref: str = "latest",
    source: str | None = None,
    say: Callable[[str], None] = print,
    *,
    dry_run: bool = False,
) -> int:
    """Upgrade the global CLI, every project in the workspace, and this checkout when it is not listed.

    One download covers every target. One failure does not stop the others. A missing global install
    or an empty workspace is reported and the rest still runs.
    """
    home: Path | None = global_install()
    group: workspace_mod.Workspace | None = _workspace()
    projects: list[workspace_mod.Project] = group.projects if group is not None else []
    checkout: Path | None = _installed_checkout()
    covered: set[Path] = {project.path for project in projects}
    extra: Path | None = None if checkout is None or checkout.resolve() in covered else checkout.resolve()
    if home is None and not projects and extra is None:
        raise Refused("nothing to upgrade. Install the factory in a project, or run `install.sh --global`.")
    if home is None:
        say("warn  no global install. `install.sh --global` installs mfactory for your user.")
    if not projects:
        say("no projects in the workspace. Run `mfactory workspace add` inside a project.")
    with _release_root(ref, source) as root:
        facts: tuple[str, object] = _release_facts(root)
        after: str = facts[0]
        need: object = facts[1]
        targets: list[Target] = []
        if home is not None:
            targets.append(_global_target(home, after))
        if group is not None:
            for project in projects:
                targets.append(_project_target(project.path, group.label(project), after, need))
        if extra is not None:
            targets.append(_project_target(extra, extra.name, after, need))
        return _run_upgrade(targets, root, dry_run, say)
