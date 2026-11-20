"""Install, upgrade, uninstall and status. The commands a person runs; agents do not."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .. import config
from .. import fleet as fleet_mod
from .. import sync as syncer
from ..install import fetch_release, layout, project_cli, upgrade
from ..util import CORE, SOURCE_REPO, ConfigError, FactoryError, Refused, is_generated, run, version


def global_home() -> Path | None:
    """The directory `install.sh --global` wrote, when this process is that copy."""
    home: Path = CORE.parent
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
    """A short status for one subscribed project, using that project's own `sync --check`."""
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
    """One row per subscribed project."""
    group: fleet_mod.Fleet = fleet_mod.Fleet.find()
    if not group.projects:
        say("no projects yet. Run `mfactory fleet subscribe` inside a project.")
        return 0
    latest: str | None = latest_release()
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
        else:
            release = f"latest {latest}"
        say(f"{group.label(project)}  {summary}  {release}")
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
        group: fleet_mod.Fleet = fleet_mod.Fleet.find(repo)
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


def upgrade_self(ref: str = "latest", source: str | None = None, say: Callable[[str], None] = print) -> None:
    """Replace the global CLI tree. Refuses a project copy and a source checkout."""
    home: Path | None = global_home()
    if home is None:
        raise Refused("this is not the global install. Inside a project, run `mfactory upgrade`. "
                      "`mfactory upgrade --self` updates the CLI installed with `install.sh --global`.")
    before: str = version()
    with tempfile.TemporaryDirectory() as tmp:
        root: Path = Path(source).resolve() if source else fetch_release(ref, tmp)
        core, adapters, licence = layout(root)
        after: str = (core / "VERSION").read_text().strip()
        tree: Path = Path(tmp) / "next"
        tree.mkdir()
        skip = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(core, tree / "core", ignore=skip)
        shutil.copytree(adapters, tree / "adapters", ignore=skip)
        for name in ("LICENSE", "NOTICE"):
            src: Path = licence / name
            if src.is_file():
                shutil.copy2(src, tree / name)
        (tree / ".global").write_text(f"{home}\n")
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
    say(f"ok    global install {before} -> {after}  {home}")


def upgrade_all(ref: str = "latest", source: str | None = None, say: Callable[[str], None] = print) -> int:
    """Upgrade every subscribed project. One failure does not stop the others."""
    group: fleet_mod.Fleet = fleet_mod.Fleet.find()
    if not group.projects:
        say("no projects yet. Run `mfactory fleet subscribe` inside a project.")
        return 0
    failed: int = 0
    for project in group.projects:
        label: str = group.label(project)

        def report(line: str, label: str = label) -> None:
            say(f"{label}  {line}")

        try:
            upgrade(project.path, ref, source, say=report)
        except FactoryError as error:
            say(f"{label}  FAIL  {error}")
            failed += 1
    return 1 if failed else 0
