"""Read each project's jobs, graph, and config for the workspace board."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from .. import config, graph, plan_usage
from ..store import SCHEMA as DATA_SCHEMA
from ..util import FactoryError, first_line
from .paths import SERVED
from .project import Project


def file_rows(root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in SERVED:
            rows.append({
                "path": str(path.relative_to(root)),
                "size": path.stat().st_size,
                "first_line": first_line(path) if path.suffix == ".md" else None,
            })
    return rows


def read_graph(project: Project) -> dict[str, object] | None:
    """Pipeline for the workspace board. None when graph.json is missing or not a graph."""
    path: Path = project.jobs_dir / "graph.json"
    if not path.is_file():
        return None
    try:
        text: str = path.read_text()
    except OSError:
        return None
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    steps: object = loaded.get("steps")
    ends: object = loaded.get("end_states")
    if not isinstance(steps, list) or not isinstance(ends, list):
        return None
    for step in steps:
        if not isinstance(step, dict):
            return None
        name: object = step.get("name")
        edges: object = step.get("edges")
        if not isinstance(name, str) or not name or not isinstance(edges, list):
            return None
    return loaded


def graph_from_config(project: Project) -> tuple[dict[str, object] | None, str | None]:
    """Pipeline from factory.toml when the checkout has not written graph.json.

    An empty job list still has a pipeline. A missing factory.toml is not an error.
    """
    path: Path = project.path / config.FILE
    if not path.is_file():
        return None, None
    try:
        cfg: config.Config = config.load(project.path, validate=False)
        drawn: object = graph.as_json(cfg)
    except FactoryError as error:
        return None, str(error)
    if not isinstance(drawn, dict):
        return None, "factory.toml did not produce a pipeline"
    pipeline: dict[str, object] = drawn
    return pipeline, None


def read_config(project: Project) -> tuple[list[dict[str, object]] | None, str | None]:
    """Rows from ``config.json``. A missing file is ``(None, None)``. A bad file is ``(None, error)``."""
    path: Path = project.jobs_dir / "config.json"
    if not path.is_file():
        return None, None
    try:
        text: str = path.read_text()
    except OSError as error:
        return None, f"config.json: {error.strerror or 'could not be read'}"
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError as error:
        return None, f"config.json: {error}"
    if not isinstance(loaded, list):
        return None, "config.json is not a list"
    rows: list[dict[str, object]] = []
    for row in loaded:
        key: object = row.get("key") if isinstance(row, dict) else None
        source: object = row.get("source") if isinstance(row, dict) else None
        if not isinstance(row, dict) or not isinstance(key, str) or not isinstance(source, str) or "value" not in row:
            return None, "config.json is not a list of settings"
        rows.append({"key": key, "value": row["value"], "source": source})
    return rows, None


def _text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _key(value: object) -> int | str | None:
    """An issue's key: a number on GitHub, text on a tracker that names issues."""
    if isinstance(value, str):
        return value or None
    return _number(value)


def normalize_job(raw: object) -> dict[str, object] | None:
    """One job with the fields the page reads, whatever factory version wrote it. None without an id."""
    if not isinstance(raw, dict):
        return None
    ident: object = raw.get("id")
    if not isinstance(ident, str) or not ident:
        return None
    job: dict[str, object] = dict(raw)
    job["title"] = _text(raw.get("title"), ident)
    job["step"] = _text(raw.get("step"), "")
    job["issue"] = _key(raw.get("issue"))
    job["round"] = _number(raw.get("round")) or 0
    job["updated_at"] = _text(raw.get("updated_at"), "")
    history: object = raw.get("history")
    job["history"] = [entry for entry in history if isinstance(entry, dict)] if isinstance(history, list) else []
    files: object = raw.get("files")
    job["files"] = [row for row in files if isinstance(row, dict)] if isinstance(files, list) else []
    return job


def schema_notice(schema: object, has_jobs: bool) -> tuple[str | None, bool]:
    """(notice for the page, whether the jobs can be shown) for a project's job files."""
    if isinstance(schema, int) and not isinstance(schema, bool):
        if schema > DATA_SCHEMA:
            return f"written by a newer factory (schema {schema}). Run `mfactory upgrade --self`.", False
        if schema == DATA_SCHEMA:
            return None, True
    if not has_jobs:
        return None, True
    return "written by an older factory. Run `mfactory upgrade` in this project.", True


def _jobs_on_disk(project: Project, jobs: list[dict[str, object]]) -> list[dict[str, object]]:
    """Indexed jobs whose folder still has job.json. A deleted folder stays off the board."""
    present: list[dict[str, object]] = []
    for job in jobs:
        ident: object = job.get("id")
        if isinstance(ident, str) and (project.jobs_dir / ident / "job.json").is_file():
            present.append(job)
    return present


def read_jobs(project: Project) -> dict[str, object]:
    index_path: Path = project.jobs_dir / "index.json"
    if index_path.is_file():
        try:
            loaded: object = json.loads(index_path.read_text())
        except json.JSONDecodeError as error:
            return {"jobs": [], "error": f"index.json: {error}"}
        if isinstance(loaded, dict):
            result: dict[str, object] = dict(loaded)
            raw_jobs: object = loaded.get("jobs")
            listed: list[object] = raw_jobs if isinstance(raw_jobs, list) else []
            normalized: list[dict[str, object]] = [job for job in map(normalize_job, listed) if job is not None]
            result["jobs"] = _jobs_on_disk(project, normalized)
            result["error"] = None
            return result
        return {"jobs": [], "error": "index.json is not an object"}
    jobs: list[dict[str, object]] = []
    schemas: set[object] = set()
    if project.jobs_dir.is_dir():
        for child in sorted(project.jobs_dir.iterdir()):
            job_file: Path = child / "job.json"
            if not job_file.is_file():
                continue
            try:
                loaded_job: object = json.loads(job_file.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(loaded_job, dict):
                loaded_job["files"] = file_rows(child)
            job: dict[str, object] | None = normalize_job(loaded_job)
            if job is not None:
                schemas.add(job.get("schema"))
                jobs.append(job)
    found: object = schemas.pop() if len(schemas) == 1 else None
    return {"jobs": jobs, "project": {}, "error": None, "schema": found}


def build_snapshot(projects: list[Project], label_of: Callable[[Project], str]) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for project in projects:
        label: str = label_of(project)
        body: dict[str, object] = read_jobs(project)
        error: object = body.get("error")
        if not project.binary.is_file():
            missing: str = ".factory/bin/mfactory is missing"
            error = f"{error}; {missing}" if isinstance(error, str) and error else missing
        description: str = ""
        info: object = body.get("project")
        if isinstance(info, dict) and isinstance(info.get("description"), str):
            description = info["description"]
        jobs: object = body.get("jobs")
        listed_jobs: list[object] = jobs if isinstance(jobs, list) else []
        notice: str | None
        shown: bool
        notice, shown = schema_notice(body.get("schema"), bool(listed_jobs))
        version_raw: object = body.get("version")
        version: str = version_raw if isinstance(version_raw, str) else ""
        generated_raw: object = body.get("generated_at")
        generated_at: str = generated_raw if isinstance(generated_raw, str) else ""
        tools: list[str] = []
        tools_raw: object = body.get("tools")
        if isinstance(tools_raw, list):
            for tool in tools_raw:
                if isinstance(tool, str):
                    tools.append(tool)
        labels: dict[str, str] = {}
        labels_raw: object = body.get("labels")
        if isinstance(labels_raw, dict):
            for key, value in labels_raw.items():
                if isinstance(key, str) and isinstance(value, str):
                    labels[key] = value
        tracker: dict[str, str] = {}
        tracker_raw: object = body.get("tracker")
        if isinstance(tracker_raw, dict):
            for key, value in tracker_raw.items():
                if isinstance(key, str) and isinstance(value, str):
                    tracker[key] = value
        concurrency_raw: object = body.get("concurrency")
        concurrency: int | None = None
        if isinstance(concurrency_raw, int) and not isinstance(concurrency_raw, bool) and concurrency_raw >= 1:
            concurrency = concurrency_raw
        config_rows: list[dict[str, object]] | None
        config_error: str | None
        config_rows, config_error = read_config(project)
        if config_error:
            error = f"{error}; {config_error}" if isinstance(error, str) and error else config_error
        item: dict[str, object] = {
            "id": label,
            "name": project.path.name,
            "path": str(project.path),
            "description": description,
            "error": error,
            "notice": notice,
            "jobs": listed_jobs if shown else [],
            "version": version,
            "generated_at": generated_at,
            "tools": tools,
            "labels": labels,
            "tracker": tracker,
            "concurrency": concurrency,
            "config": config_rows,
        }
        pipeline: dict[str, object] | None = read_graph(project)
        if pipeline is None:
            fallback: dict[str, object] | None
            fallback_error: str | None
            fallback, fallback_error = graph_from_config(project)
            if fallback_error:
                error = f"{error}; {fallback_error}" if isinstance(error, str) and error else fallback_error
                item["error"] = error
            pipeline = fallback
        if pipeline is not None:
            item["graph"] = pipeline
        items.append(item)
    return {"projects": items, "plan_usage": plan_usage.read()}
