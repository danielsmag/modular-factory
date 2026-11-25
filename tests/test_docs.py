import re
from pathlib import Path

_ROOT: Path = Path(__file__).resolve().parents[1]


def test_the_readme_status_line_names_no_version_or_test_count():
    readme: str = (_ROOT / "README.md").read_text(encoding="utf-8")
    status: list[str] = [line for line in readme.splitlines() if "**Status:" in line]
    assert len(status) == 1
    start: int = readme.index(status[0])
    block: str = readme[start : readme.index("\n\n", start)]
    assert re.search(r"\d+\.\d+\.\d+", block) is None
    assert re.search(r"\b\d+ automated tests\b", block) is None
