"""The dashboard's Markdown renderer (web/markdown.js), run under node with a fake element factory."""

import html
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web" / "markdown.js"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node to run web/markdown.js")

# The fake `make` has el()'s signature and returns plain objects instead of DOM nodes.
DRIVER = """
const make = (tag, props, ...kids) => ({
  tag, props: props || {},
  kids: kids.flat(Infinity).filter((kid) => kid !== null && kid !== undefined && kid !== false),
});
let input = "";
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  const { call, text } = JSON.parse(input);
  const out = call === "split" ? splitHeader(text) : renderMarkdown(text, make);
  process.stdout.write(JSON.stringify(out));
});
"""


def run_node(call: str, text: str):
    script = SOURCE.read_text() + "\n" + DRIVER
    result = subprocess.run(
        ["node", "-e", script], input=json.dumps({"call": call, "text": text}),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def serialize(node) -> str:
    if isinstance(node, str):
        return html.escape(node, quote=False)
    props = dict(node["props"])
    text = props.pop("text", None)
    attrs = "".join(f' {key}="{html.escape(str(props[key]))}"' for key in sorted(props))
    inner = html.escape(text, quote=False) if text is not None else ""
    inner += "".join(serialize(kid) for kid in node["kids"])
    return f"<{node['tag']}{attrs}>{inner}</{node['tag']}>"


def render(md: str) -> str:
    return "".join(serialize(node) for node in run_node("render", md))


def split(md: str) -> dict:
    return run_node("split", md)


# ---- inline -----------------------------------------------------------------

def test_headings_and_paragraphs():
    assert render("# Title\n\nSome text\nwraps here.\n\n### Small") == \
        "<h1>Title</h1><p>Some text wraps here.</p><h3>Small</h3>"


def test_inline_spans():
    assert render("a **b** _c_ `d*e*` [f](https://x.io/a?b=1)") == (
        "<p>a <strong>b</strong> <em>c</em> <code>d*e*</code> "
        '<a href="https://x.io/a?b=1" rel="noopener noreferrer" target="_blank">f</a></p>')


def test_autolink_and_escapes():
    assert render("<https://x.io> \\*not\\*") == \
        '<p><a href="https://x.io" rel="noopener noreferrer" target="_blank">https://x.io</a> *not*</p>'


def test_raw_html_is_text():
    assert render('<script>alert(1)</script> <img src=x onerror="y">') == \
        '<p>&lt;script&gt;alert(1)&lt;/script&gt; &lt;img src=x onerror="y"&gt;</p>'


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "//evil.io", "spec.md"])
def test_only_http_links_become_links(url):
    assert render(f"[go]({url})") == "<p>go</p>"


def test_underscores_inside_words_stay_literal():
    assert render("call file_rows and __init__ and snake_case_name") == \
        "<p>call file_rows and __init__ and snake_case_name</p>"


@pytest.mark.parametrize("md", ["**bold", "a ` b", "[label](", "_open", "[x]"])
def test_unclosed_inline_markers_stay_literal(md):
    assert render(md) == f"<p>{html.escape(md, quote=False)}</p>"


def test_crlf_is_the_same_as_lf():
    assert render("# A\r\n\r\ntext\r\n") == render("# A\n\ntext\n")


# ---- blocks -----------------------------------------------------------------

def test_lists_and_nesting():
    assert render("- a\n- b\n  - c\n\n3. x\n4. y") == \
        '<ul><li>a</li><li>b<ul><li>c</li></ul></li></ul><ol start="3"><li>x</li><li>y</li></ol>'


def test_list_items_use_inline_formatting():
    assert render("1. `npm test` passes") == "<ol><li><code>npm test</code> passes</li></ol>"


def test_fenced_code_is_literal():
    assert render("```py\nx = '<b>' * 2\n  # **no**\n```") == \
        "<pre><code>x = '&lt;b&gt;' * 2\n  # **no**</code></pre>"


def test_unclosed_fence_runs_to_end():
    assert render("text\n\n```\nline 1\nline 2") == "<p>text</p><pre><code>line 1\nline 2</code></pre>"


def test_blockquote():
    assert render("> Please add **this**.\n> Thanks") == \
        "<blockquote><p>Please add <strong>this</strong>. Thanks</p></blockquote>"


def test_table():
    assert render("| a | b |\n|---|:-:|\n| `1` | 2 |\n| 3 |") == (
        '<div class="md-table"><table><thead><tr><th>a</th><th>b</th></tr></thead>'
        "<tbody><tr><td><code>1</code></td><td>2</td></tr><tr><td>3</td><td></td></tr></tbody></table></div>")


def test_rule_and_a_pipe_line_without_separator_is_a_paragraph():
    assert render("a | b\n\n---\n\nc") == "<p>a | b</p><hr></hr><p>c</p>"


@pytest.mark.parametrize("unit", ["*a ", "_a ", "**a ", "[a ", "[a]("])
def test_a_long_line_of_unmatched_markers_renders_quickly(unit):
    # The preview re-renders on every live refresh, so a 30 KB line must not take seconds.
    line = unit * (30000 // len(unit))
    started = time.monotonic()
    assert render(line).startswith("<p>")
    assert time.monotonic() - started < 1.0
