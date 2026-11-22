"""Read each project's jobs, graph, and config for the fleet board."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from ..util import first_line
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
    """Pipeline for the fleet board. None when graph.json is missing or not a graph."""
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


def read_jobs(project: Project) -> dict[str, object]:
    index_path: Path = project.jobs_dir / "index.json"
    if index_path.is_file():
        try:
            loaded: object = json.loads(index_path.read_text())
        except json.JSONDecodeError as error:
            return {"jobs": [], "error": f"index.json: {error}"}
        if isinstance(loaded, dict):
            result: dict[str, object] = dict(loaded)
            result["error"] = None
            return result
        return {"jobs": [], "error": "index.json is not an object"}
    jobs: list[dict[str, object]] = []
    if project.jobs_dir.is_dir():
        for child in sorted(project.jobs_dir.iterdir()):
            job_file: Path = child / "job.json"
            if not job_file.is_file():
                continue
            try:
                job: object = json.loads(job_file.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(job, dict):
                job["files"] = file_rows(child)
                jobs.append(job)
    return {"jobs": jobs, "project": {}, "error": None}


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
            "jobs": jobs if isinstance(jobs, list) else [],
            "version": version,
            "generated_at": generated_at,
            "tools": tools,
            "labels": labels,
            "concurrency": concurrency,
            "config": config_rows,
        }
        graph: dict[str, object] | None = read_graph(project)
        if graph is not None:
            item["graph"] = graph
        items.append(item)
    return {"projects": items}
