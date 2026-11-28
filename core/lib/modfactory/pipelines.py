"""Pipelines as YAML: read the project's pipelines/ folder, check it against the schema, resolve `extends`.

A pipe file declares one or more pipelines, "pipes", under `pipes:`. Each has a
`start` step, an `escalate_to` step and its `steps`, each step with the outcomes
it can end with (`on`). The built-in `default` pipeline ships in
`core/pipelines/default.yaml` and is used when the project declares none of
that name. A pipeline that `extends` another starts from it and overrides it
step by step, key by key, as factory.toml once overrode the defaults.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import jsonschema_lite
from .util import CORE, FactoryError

DEFAULT = "default"
BUILTIN_REF = "factory:default"
BUILTIN_FILE = CORE / "pipes" / "default.yaml"
SCHEMA_FILE = CORE / "schema" / "pipe.schema.json"
KEY = "pipes"               # the top-level key of a pipe file
FOLDER = "factory/pipes"    # where a project's pipe files are, from the repository root
# Where 0.32.0 kept them, and the key their files used. Both still load until `mfactory migrate layout`.
OLD_KEY = "pipelines"
OLD_FOLDER = "pipelines"
BUILTIN_LABEL = "default"   # the source label of a value that comes from the built-in pipeline
SUFFIXES = (".yaml", ".yml")


@dataclass
class Pipeline:
    name: str
    description: str
    file: str                    # where it is declared: a path relative to the repository, or "built-in"
    start: str
    escalate_to: str
    steps: dict[str, Any]
    extends: str | None = None
    sources: dict[tuple[str, ...], str] = field(default_factory=dict)  # step key path -> file it came from

    def graph_data(self) -> dict[str, Any]:
        """The shape graph.problems() reads."""
        return {"steps": self.steps, "run": {"start": self.start, "escalate_to": self.escalate_to}}


@dataclass
class PipelineFile:
    path: str                    # relative to the repository, or "built-in"
    text: str
    names: list[str]
    errors: list[str]
    old_key: bool = False        # declared under `pipelines:`, as 0.32.0 wrote it


# ---- YAML ------------------------------------------------------------------


def yaml_module() -> Any:
    try:
        import yaml
    except ImportError as e:
        raise FactoryError("PyYAML is required to read pipelines: run `python3 -m pip install --user pyyaml`") from e
    return yaml


_LOADER: Any = None


def loader() -> Any:
    """A safe loader with YAML 1.2 booleans: only true and false, so `on:` stays a key and `no` stays a word.

    It also refuses a key given twice in one mapping, which YAML would otherwise let the last one win.
    """
    global _LOADER
    if _LOADER is not None:
        return _LOADER
    yaml = yaml_module()
    import re

    class Loader(yaml.SafeLoader):
        pass

    Loader.yaml_implicit_resolvers = {
        first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:bool"]
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
                                 list("tTfF"))

    def mapping(self: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key: Any = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"{key!r} is given twice in one mapping", key_node.start_mark)
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(self, node, deep)

    Loader.construct_mapping = mapping  # type: ignore[method-assign]
    _LOADER = Loader
    return Loader


def parse(text: str, label: str) -> Any:
    yaml = yaml_module()
    try:
        return yaml.load(text, Loader=loader())  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as e:
        mark: Any = getattr(e, "problem_mark", None)
        where: str = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
        problem: str = getattr(e, "problem", None) or str(e).splitlines()[0]
        context: str = getattr(e, "context", None) or ""
        return _ParseError(f"{label}{where}: {problem}" + (f" {context}" if context and context not in problem else ""))


class _ParseError:
    def __init__(self, message: str):
        self.message = message


def dump(data: Any) -> str:
    """YAML text for data the factory writes, such as `migrate pipelines`."""
    yaml = yaml_module()

    class Dumper(yaml.SafeDumper):
        pass

    def edge(dumper: Any, value: dict[str, Any]) -> Any:
        flow: bool = set(value) <= {"to", "max"} and "to" in value
        return dumper.represent_mapping("tag:yaml.org,2002:map", value.items(), flow_style=flow or None)

    Dumper.add_representer(dict, edge)
    return yaml.dump(data, Dumper=Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100)


_SCHEMA: dict[str, Any] | None = None


def schema() -> dict[str, Any]:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads(SCHEMA_FILE.read_text())
    return _SCHEMA


# ---- Merging ---------------------------------------------------------------


def normalise_edges(steps: dict[str, Any]) -> dict[str, Any]:
    """Write every edge as a mapping, so `FAIL: build` and `FAIL: { max: 3 }` both merge."""
    out: dict[str, Any] = {}
    for name, step in steps.items():
        if isinstance(step, dict) and isinstance(step.get("on"), dict):
            step = dict(step, on={k: {"to": v} if isinstance(v, str) else v for k, v in step["on"].items()})
        out[name] = step
    return out


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Mappings merge key by key; any other value replaces what was there."""
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        both: bool = isinstance(value, dict) and isinstance(out.get(key), dict)
        out[key] = merge(out[key], value) if both else value
    return out


def _leaves(table: dict[str, Any], prefix: tuple[str, ...] = ()) -> Any:
    for key, value in table.items():
        if isinstance(value, dict) and value:
            yield from _leaves(value, prefix + (key,))
        else:
            yield prefix + (key,), value


def _mark(sources: dict[tuple[str, ...], str], layer: dict[str, Any], label: str) -> None:
    for path, _ in _leaves(layer):
        for old in [p for p in sources if p[:len(path)] == path or path[:len(p)] == p]:
            del sources[old]
        sources[path] = label


# ---- Loading ---------------------------------------------------------------


def folder(repo: Path, setting: object) -> Path:
    """The folder the project's pipes are read from: the setting, factory/pipes, or 0.32.0's pipelines/."""
    if isinstance(setting, str) and setting.strip():
        return Path(repo) / setting
    if old_folder_in_use(repo):
        return Path(repo) / OLD_FOLDER
    return Path(repo) / FOLDER


def _yaml_in(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_file() and p.suffix in SUFFIXES)


def old_folder_in_use(repo: Path) -> bool:
    """True when the pipes are still in pipelines/ at the root, where 0.32.0 put them, and factory/pipes/ has none."""
    return not _yaml_in(Path(repo) / FOLDER) and bool(_yaml_in(Path(repo) / OLD_FOLDER))


def project_files(repo: Path, setting: object) -> list[Path]:
    return _yaml_in(folder(repo, setting))


def _read(path: Path, label: str) -> tuple[dict[str, Any], PipelineFile]:
    """The pipelines one file declares, and the file's report. A broken file declares nothing."""
    text: str = path.read_text()
    doc: Any = parse(text, label)
    if isinstance(doc, _ParseError):
        return {}, PipelineFile(label, text, [], [doc.message])
    if doc is None:
        return {}, PipelineFile(label, text, [], [f"{label}: the file is empty. It must declare `pipes:`"])
    old: bool = isinstance(doc, dict) and OLD_KEY in doc and KEY not in doc
    if old:  # a file from 0.32.0, under `pipelines:`
        doc = {KEY if key == OLD_KEY else key: value for key, value in doc.items()}
    errors: list[str] = [f"{label}: {where or '(top)'} {message}"
                         for where, message in jsonschema_lite.validate(doc, schema())]
    declared: dict[str, Any] = doc.get(KEY) if isinstance(doc, dict) else None  # type: ignore[assignment]
    names: list[str] = list(declared) if isinstance(declared, dict) else []
    if errors:
        return {}, PipelineFile(label, text, names, errors, old)
    return dict(declared), PipelineFile(label, text, names, [], old)


def load_all(repo: Path, setting: object = None, legacy_steps: dict[str, Any] | None = None,
             legacy_run: dict[str, Any] | None = None, legacy_label: str = "factory.toml",
             ) -> tuple[dict[str, Pipeline], list[PipelineFile], list[str]]:
    """Every pipeline of the project, the files read, and the problems found.

    `legacy_steps` and `legacy_run` are `[steps.*]` and `[run].start`/`escalate_to`
    from factory.toml, which older projects still have. They override `default`.
    """
    repo = Path(repo)
    builtin_decl, builtin_file = _read(BUILTIN_FILE, "built-in")
    if builtin_file.errors:
        raise FactoryError("the built-in pipeline is broken: " + "; ".join(builtin_file.errors))
    builtin_file.path = "built-in"

    problems: list[str] = []
    files: list[PipelineFile] = []
    declared: dict[str, tuple[dict[str, Any], str]] = {}
    for path in project_files(repo, setting):
        label: str = path.relative_to(repo).as_posix()
        found, report = _read(path, label)
        files.append(report)
        problems += report.errors
        for name, decl in found.items():
            if name in declared:
                problems.append(f'{label}: pipe "{name}" is also declared in {declared[name][1]}')
                continue
            declared[name] = (decl if isinstance(decl, dict) else {}, label)

    resolved: dict[str, Pipeline] = {}

    def build(name: str, decl: dict[str, Any], label: str, base: Pipeline | None) -> Pipeline:
        steps: dict[str, Any] = dict(base.steps) if base else {}
        sources: dict[tuple[str, ...], str] = dict(base.sources) if base else {}
        own: dict[str, Any] = normalise_edges(decl.get("steps") or {})
        steps = merge(steps, own)
        _mark(sources, own, BUILTIN_LABEL if label == "built-in" else label)
        return Pipeline(
            name=name, file=label, extends=decl.get("extends"),
            description=str(decl.get("description", base.description if base else "") or ""),
            start=str(decl.get("start") or (base.start if base else "")),
            escalate_to=str(decl.get("escalate_to") or (base.escalate_to if base else "")),
            steps=steps, sources=sources)

    builtin: Pipeline = build(DEFAULT, builtin_decl[DEFAULT], "built-in", None)

    def resolve(name: str, chain: list[str]) -> Pipeline | None:
        if name in resolved:
            return resolved[name]
        if name in chain:
            problems.append(f"pipes extend each other in a loop: {' -> '.join(chain + [name])}")
            return None
        if name not in declared:
            return builtin if name == DEFAULT else None
        decl, label = declared[name]
        base: Pipeline | None = None
        parent: object = decl.get("extends")
        if parent == BUILTIN_REF:
            base = builtin
        elif isinstance(parent, str):
            base = resolve(parent, chain + [name])
            if base is None:
                if parent not in declared and parent != DEFAULT:
                    problems.append(f'{label}: pipe "{name}" extends "{parent}", which is not a pipe. '
                                    f"Pipes: {', '.join(sorted(set(declared) | {DEFAULT}))}")
                return None
        pipeline: Pipeline = build(name, decl, label, base)
        if name == DEFAULT:
            pipeline = _with_legacy(pipeline, legacy_steps, legacy_run, legacy_label)
        resolved[name] = pipeline
        return pipeline

    if DEFAULT not in declared:
        resolved[DEFAULT] = _with_legacy(builtin, legacy_steps, legacy_run, legacy_label)
    for name in [DEFAULT, *declared]:
        resolve(name, [])
    for name, pipeline in resolved.items():
        where: str = pipeline.file if pipeline.file != "built-in" else "the built-in pipe"
        if not pipeline.steps:
            problems.append(f'{where}: pipe "{name}" has no steps')
        for key in ("start", "escalate_to"):
            if not getattr(pipeline, key):
                problems.append(f'{where}: pipe "{name}" needs {key}, or extends a pipe that has it')
    ordered: dict[str, Pipeline] = {DEFAULT: resolved[DEFAULT]} if DEFAULT in resolved else {}
    ordered.update({n: p for n, p in resolved.items() if n != DEFAULT})
    return ordered, files, problems


def _with_legacy(pipeline: Pipeline, steps: dict[str, Any] | None, run: dict[str, Any] | None,
                 label: str) -> Pipeline:
    if not steps and not run:
        return pipeline
    own: dict[str, Any] = normalise_edges(steps or {})
    sources: dict[tuple[str, ...], str] = dict(pipeline.sources)
    _mark(sources, own, label)
    return Pipeline(
        name=pipeline.name, description=pipeline.description, file=pipeline.file, extends=pipeline.extends,
        start=str((run or {}).get("start") or pipeline.start),
        escalate_to=str((run or {}).get("escalate_to") or pipeline.escalate_to),
        steps=merge(pipeline.steps, own), sources=sources)


_BUILTIN_STEPS: dict[str, Any] | None = None


def builtin_step(name: str) -> dict[str, Any]:
    """A step of the built-in default pipeline, as shipped."""
    global _BUILTIN_STEPS
    if _BUILTIN_STEPS is None:
        doc: Any = parse(BUILTIN_FILE.read_text(), "built-in")
        _BUILTIN_STEPS = normalise_edges(doc[KEY][DEFAULT]["steps"]) if isinstance(doc, dict) else {}
    found: object = _BUILTIN_STEPS.get(name)
    return found if isinstance(found, dict) else {}


def starter_text() -> str:
    """The factory/pipes/default.yaml that `mfactory install` writes: the built-in pipe, spelled out."""
    text: str = BUILTIN_FILE.read_text()
    _, _, body = text.partition(f"\n{KEY}:\n")
    intro: str = (
        "# yaml-language-server: $schema=../../.factory/schema/pipe.schema.json\n"
        "#\n"
        "# This project's pipes: its pipelines. Each file in this folder declares one or more.\n"
        "# A job runs the pipe named by `--pipe`, or by [run].pipe in factory/factory.toml.\n"
        "# Reference: https://danielsmag.github.io/modular-factory/docs/pipeline.html\n"
        "#\n"
        "#   .factory/bin/mfactory pipes validate      check every file here against the schema\n"
        "#   .factory/bin/mfactory graph --pipe NAME   print one pipe\n"
        "#\n"
        "# Each step names its type and where each outcome leads. `max` on an edge is a\n"
        "# loop limit: when it is used up the job goes to `escalate_to` instead.\n"
        "#\n"
        "# Add another pipe in a new file, such as factory/pipes/hotfix.yaml:\n"
        "#\n"
        "#   pipes:\n"
        "#     hotfix:\n"
        "#       description: Small fixes, no spec\n"
        "#       extends: default\n"
        "#       steps:\n"
        "#         spec: { enabled: false, skip: DONE }\n"
    )
    return intro + f"\n{KEY}:\n" + body
