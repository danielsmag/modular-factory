"""Jira's document format: reading it as plain text, and writing the factory's markdown as a document."""

from modfactory.service.tracker import adf


def text(value):
    return {"type": "text", "text": value}


def doc(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


def para(*inline):
    return {"type": "paragraph", "content": list(inline)}


# ---- Reading -------------------------------------------------------------------


def test_nothing_reads_as_empty_text():
    assert adf.to_text(None) == ""
    assert adf.to_text(doc()) == ""


def test_paragraphs_are_separated_by_a_blank_line():
    assert adf.to_text(doc(para(text("One.")), para(text("Two.")))) == "One.\n\nTwo."


def test_a_command_typed_as_a_comment_reads_back_exactly():
    assert adf.to_text(doc(para(text("/mfactory approve")))) == "/mfactory approve"


def test_a_line_break_inside_a_paragraph_is_kept():
    found = adf.to_text(doc(para(text("/mfactory changes"), {"type": "hardBreak"}, text("Use the old name."))))
    assert found == "/mfactory changes\nUse the old name."


def test_headings_lists_and_code_read_as_markdown():
    found = adf.to_text(doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [text("Scope")]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [para(text("first"))]},
            {"type": "listItem", "content": [para(text("second"))]}]},
        {"type": "orderedList", "content": [{"type": "listItem", "content": [para(text("step"))]}]},
        {"type": "codeBlock", "attrs": {"language": "python"}, "content": [text("print(1)")]},
        {"type": "rule"}))
    assert found == "## Scope\n\n- first\n- second\n\n1. step\n\n```python\nprint(1)\n```\n\n---"


def test_mentions_links_and_emoji_read_as_their_text():
    found = adf.to_text(doc(para(
        {"type": "mention", "attrs": {"id": "abc", "text": "@Alice"}}, text(" see "),
        {"type": "inlineCard", "attrs": {"url": "https://example.test/x"}}, text(" "),
        {"type": "emoji", "attrs": {"shortName": ":tada:", "text": "🎉"}},
        {"type": "text", "text": " docs", "marks": [{"type": "link", "attrs": {"href": "https://example.test/d"}}]})))
    assert found == "@Alice see https://example.test/x 🎉 docs (https://example.test/d)"


def test_a_node_this_reader_does_not_know_still_gives_its_text():
    found = adf.to_text(doc({"type": "panel", "attrs": {"panelType": "info"}, "content": [para(text("Note."))]},
                            {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "1"}}]}))
    assert found == "Note."


# ---- Writing -------------------------------------------------------------------


def test_plain_words_become_one_paragraph():
    assert adf.from_markdown("Hello there.") == doc(para(text("Hello there.")))


def test_the_factorys_ask_becomes_a_heading_paragraphs_a_rule_and_a_list():
    written = adf.from_markdown(
        "### Factory: approve the spec\n\nStep `spec-approval` of job `001-a`.\n\n---\n"
        "To reply, comment on this issue:\n- `/mfactory approve`\n- `/mfactory reject <why>`")
    kinds = [block["type"] for block in written["content"]]
    assert kinds == ["heading", "paragraph", "rule", "paragraph", "bulletList"]
    assert written["content"][0]["attrs"] == {"level": 3}
    assert len(written["content"][4]["content"]) == 2


def test_bold_and_code_keep_their_marks():
    written = adf.from_markdown("**spec.md** says `x = 1`.")
    assert written["content"][0]["content"] == [
        {"type": "text", "text": "spec.md", "marks": [{"type": "strong"}]},
        text(" says "),
        {"type": "text", "text": "x = 1", "marks": [{"type": "code"}]},
        text(".")]


def test_a_fenced_block_is_a_code_block_and_its_text_is_untouched():
    written = adf.from_markdown("Output:\n\n```\n**not bold**\n- not a list\n```\n")
    assert written["content"][1] == {"type": "codeBlock", "content": [text("**not bold**\n- not a list")]}


def test_what_is_written_reads_back_as_the_same_text():
    source = ("### Factory: rejected\n\nNot in scope.\n\n- one\n- two\n\n1. first\n2. second\n\n"
              "```\ncode here\n```\n\n---\n\nLast line.")
    assert adf.to_text(adf.from_markdown(source)) == source


def test_empty_text_is_a_document_jira_accepts():
    assert adf.from_markdown("") == doc(para())
