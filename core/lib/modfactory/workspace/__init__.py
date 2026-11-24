"""A local list of installed factories, and one queue across them.

Job state stays in each project. The workspace runs that project's own `factory`
binary and reads that project's `.factory/jobs/`. The dashboard server is
`workspace.service`, including a checkout that is not in the workspace file.
"""

from .concurrency import cap_rows, default_concurrency, parse_concurrency
from .jobs import file_rows, read_config, read_graph, read_jobs
from .paths import FILE, HEADER, SCHEMA, SERVED, toml_string, user_file
from .project import Project, labels_for
from .registry import Workspace

__all__ = [
    "FILE",
    "HEADER",
    "SCHEMA",
    "SERVED",
    "Workspace",
    "Project",
    "cap_rows",
    "default_concurrency",
    "file_rows",
    "labels_for",
    "parse_concurrency",
    "read_config",
    "read_graph",
    "read_jobs",
    "toml_string",
    "user_file",
]
