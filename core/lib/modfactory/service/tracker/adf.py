"""Atlassian Document Format, the shape of Jira descriptions and comments.

`to_text` reads a document as plain text, close to markdown, for issue.md and for reading replies.
`from_markdown` writes the small markdown the factory's own comments use. Neither is a full converter:
a node this file does not know is read through to its text, and unknown markdown stays plain words.
"""

import re
from typing import Any

Node = dict[str, Any]
INLINE = {"text", "hardBreak", "mention", "emoji", "inlineCard", "date", "status"}


# ---- Reading ---------------------------------------------------------------


def to_text(doc: object) -> str:
    """The document as plain text. Marks such as bold and code are dropped; a link keeps its address."""
    if not isinstance(doc, dict):
        return ""
    return _blocks(doc.get("content"))


def _children(node: Node) -> list[Node]:
    content: object = node.get("content")
    return [child for child in content if isinstance(child, dict)] if isinstance(content, list) else []


def _blocks(content: object, joiner: str = "\n\n") -> str:
    nodes: list[Node] = [node for node in content if isinstance(node, dict)] if isinstance(content, list) else []
    return joiner.join(text for text in map(_block, nodes) if text)


def _block(node: Node) -> str:
    kind: object = node.get("type")
    attrs: Node = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    children: list[Node] = _children(node)
    if kind in INLINE:
        return _inline([node])
    if kind == "paragraph":
        return _inline(children)
    if kind == "heading":
        level: object = attrs.get("level")
        return "#" * (level if isinstance(level, int) and 1 <= level <= 6 else 1) + " " + _inline(children)
    if kind in ("bulletList", "orderedList", "taskList"):
        rows: list[str] = []
        for number, item in enumerate(children, 1):
            mark: str = f"{number}. " if kind == "orderedList" else "- "
            body: str = _blocks(_children(item), "\n") or _inline(_children(item))
            rows.append(mark + body.replace("\n", "\n" + " " * len(mark)))
        return "\n".join(rows)
    if kind == "codeBlock":
        language: object = attrs.get("language")
        return f"```{language if isinstance(language, str) else ''}\n{_inline(children)}\n```"
    if kind == "rule":
        return "---"
    if kind == "blockquote":
        return "\n".join("> " + line for line in _blocks(children).splitlines())
    if kind == "tableRow":
        return " | ".join(_blocks(_children(cell), " ") for cell in children)
    if kind == "table":
        return _blocks(children, "\n")
    if children and all(child.get("type") in INLINE for child in children):
        return _inline(children)
    return _blocks(children)


def _inline(nodes: list[Node]) -> str:
    out: list[str] = []
    for node in nodes:
        kind: object = node.get("type")
        attrs: Node = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
        if kind == "text":
            text: str = str(node.get("text") or "")
            href: object = next((mark.get("attrs", {}).get("href") for mark in node.get("marks") or []
                                 if isinstance(mark, dict) and mark.get("type") == "link"), None)
            out.append(f"{text} ({href})" if isinstance(href, str) and href != text else text)
        elif kind == "hardBreak":
            out.append("\n")
        elif kind == "mention":
            out.append(str(attrs.get("text") or f"@{attrs.get('id', '')}"))
        elif kind == "emoji":
            out.append(str(attrs.get("text") or attrs.get("shortName") or ""))
        elif kind == "inlineCard":
            out.append(str(attrs.get("url") or ""))
        elif kind in ("date", "status"):
            out.append(str(attrs.get("text") or attrs.get("timestamp") or ""))
        else:
            out.append(_inline(_children(node)))
    return "".join(out)


# ---- Writing ---------------------------------------------------------------

HEADING = re.compile(r"(#{1,6})\s+(.*)")
BULLET = re.compile(r"\s*[-*]\s+(.*)")
NUMBERED = re.compile(r"\s*\d+[.)]\s+(.*)")
FENCE = re.compile(r"```\s*([A-Za-z0-9_+-]*)\s*")
MARKED = re.compile(r"(`[^`\n]+`|\*\*[^*\n]+\*\*)")
RULES = ("---", "***", "___")


def from_markdown(text: str) -> Node:
    """A document for the headings, paragraphs, lists, rules, code and bold the factory writes."""
    lines: list[str] = text.strip("\n").splitlines()
    blocks: list[Node] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append({"type": "paragraph", "content": _spans("\n".join(paragraph))})
            paragraph.clear()

    i = 0
    while i < len(lines):
        line: str = lines[i]
        fence: re.Match[str] | None = FENCE.fullmatch(line)
        heading: re.Match[str] | None = HEADING.fullmatch(line)
        if fence:
            flush()
            i += 1
            code: list[str] = []
            while i < len(lines) and not FENCE.fullmatch(lines[i]):
                code.append(lines[i])
                i += 1
            block: Node = {"type": "codeBlock"}
            if fence.group(1):
                block["attrs"] = {"language": fence.group(1)}
            if "\n".join(code):
                block["content"] = [{"type": "text", "text": "\n".join(code)}]
            blocks.append(block)
        elif heading:
            flush()
            blocks.append({"type": "heading", "attrs": {"level": len(heading.group(1))},
                           "content": _spans(heading.group(2))})
        elif line.strip() in RULES:
            flush()
            blocks.append({"type": "rule"})
        elif BULLET.fullmatch(line) or NUMBERED.fullmatch(line):
            flush()
            pattern: re.Pattern[str] = BULLET if BULLET.fullmatch(line) else NUMBERED
            items: list[Node] = []
            while i < len(lines) and (found := pattern.fullmatch(lines[i])):
                items.append({"type": "listItem",
                              "content": [{"type": "paragraph", "content": _spans(found.group(1))}]})
                i += 1
            blocks.append({"type": "bulletList" if pattern is BULLET else "orderedList", "content": items})
            continue
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
        i += 1
    flush()
    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph", "content": []}]}


def _spans(text: str) -> list[Node]:
    """Inline nodes for one block: `code`, **bold**, and a hard break at each line end."""
    out: list[Node] = []
    for number, line in enumerate(text.split("\n")):
        if number:
            out.append({"type": "hardBreak"})
        for part in MARKED.split(line):
            if not part:
                continue
            if part.startswith("`") and part.endswith("`") and len(part) > 2:
                out.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
            elif part.startswith("**") and part.endswith("**") and len(part) > 4:
                out.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]})
            else:
                out.append({"type": "text", "text": part})
    return out
