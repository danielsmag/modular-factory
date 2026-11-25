// Renders a subset of Markdown to DOM nodes for the job file preview.
// Job files carry text copied from issues, so nothing here parses HTML: every
// piece of text reaches the page as a text node through the injected `make`
// (the page passes el()), and only http(s) URLs become links.

const MD_ESCAPABLE = "\\`*_{}[]()<>#+-.!|~";
const MD_SAFE_URL = /^https?:\/\//i;

function mdIsWordChar(ch) {
  return !!ch && /[\p{L}\p{N}]/u.test(ch);
}
function mdRunLength(s, i) {
  let n = 0;
  while (s[i + n] === s[i]) n += 1;
  return n;
}
function mdLink(href, kids, make) {
  return make("a", { href, target: "_blank", rel: "noopener noreferrer" }, kids);
}
// The index of the `close` that balances the `open` at s[i], or -1.
// Link labels and URLs are short, so the search stops after MD_LINK_SPAN
// characters; unbounded, a line of lone brackets would take quadratic time.
const MD_LINK_SPAN = 2000;
function mdMatching(s, i, open, close) {
  let depth = 0;
  const stop = Math.min(s.length, i + MD_LINK_SPAN);
  for (let j = i; j < stop; j += 1) {
    if (s[j] === "\\") { j += 1; continue; }
    if (s[j] === open) depth += 1;
    else if (s[j] === close && --depth === 0) return j;
  }
  return -1;
}
// The closing delimiter for emphasis opened at i with `delim`, or -1.
function mdCloser(s, i, delim) {
  const ch = delim[0];
  for (let j = i + delim.length; j < s.length; j += 1) {
    if (s[j] === "\\") { j += 1; continue; }
    if (s[j] === "`") {
      const run = mdRunLength(s, j);
      const end = s.indexOf("`".repeat(run), j + run);
      if (end > 0) j = end + run - 1;
      continue;
    }
    if (s[j] !== ch) continue;
    const run = mdRunLength(s, j);
    const fits = run === delim.length && j > i + delim.length && !/\s/.test(s[j - 1]);
    if (fits && !(ch === "_" && mdIsWordChar(s[j + run]))) return j;
    j += run - 1;
  }
  return -1;
}

function mdInline(s, make) {
  const out = [];
  let buf = "";
  const flush = () => { if (buf) { out.push(buf); buf = ""; } };
  // A delimiter with no closer after one position has none after any later
  // position either; remembering that keeps a line of lone `*` linear.
  const unclosed = new Set();
  let i = 0;
  while (i < s.length) {
    const ch = s[i];
    if (ch === "\\" && MD_ESCAPABLE.includes(s[i + 1] || "")) {
      buf += s[i + 1];
      i += 2;
      continue;
    }
    if (ch === "`") {
      const run = mdRunLength(s, i);
      const fence = "`".repeat(run);
      let end = s.indexOf(fence, i + run);
      while (end > 0 && s[end + run] === "`") end = s.indexOf(fence, end + mdRunLength(s, end));
      if (end > 0) {
        let code = s.slice(i + run, end);
        if (code.length > 2 && code[0] === " " && code[code.length - 1] === " " && code.trim()) code = code.slice(1, -1);
        flush();
        out.push(make("code", { text: code }));
        i = end + run;
      } else {
        buf += fence;
        i += run;
      }
      continue;
    }
    if (ch === "<") {
      const auto = /^<(https?:\/\/[^\s<>]+)>/i.exec(s.slice(i));
      if (auto) {
        flush();
        out.push(mdLink(auto[1], [auto[1]], make));
        i += auto[0].length;
        continue;
      }
    }
    if (ch === "[") {
      const close = mdMatching(s, i, "[", "]");
      const end = close > 0 && s[close + 1] === "(" ? mdMatching(s, close + 1, "(", ")") : -1;
      if (end > 0) {
        const kids = mdInline(s.slice(i + 1, close), make);
        const url = s.slice(close + 2, end).trim();
        flush();
        if (MD_SAFE_URL.test(url)) out.push(mdLink(url, kids, make));
        else out.push(...kids);
        i = end + 1;
        continue;
      }
    }
    if (ch === "*" || ch === "_") {
      const run = mdRunLength(s, i);
      const delim = ch.repeat(Math.min(run, 2));
      const opens = !/\s/.test(s[i + delim.length] || " ") && !(ch === "_" && mdIsWordChar(s[i - 1]));
      const end = opens && !unclosed.has(delim) ? mdCloser(s, i, delim) : -1;
      if (opens && end < 0) unclosed.add(delim);
      const content = end > 0 ? s.slice(i + delim.length, end) : "";
      // `__name__` is a Python dunder far more often than bold text: keep it literal.
      const dunder = delim === "__" && /^\w+$/.test(content);
      if (end > 0 && !dunder) {
        flush();
        out.push(make(delim.length === 2 ? "strong" : "em", {}, mdInline(content, make)));
        i = end + delim.length;
      } else {
        buf += ch.repeat(run);
        i += run;
      }
      continue;
    }
    buf += ch;
    i += 1;
  }
  flush();
  return out;
}

const MD_HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
// A backtick fence's info string has no backticks, so ```code``` on one line stays a code span.
const MD_FENCE = /^( {0,3})(`{3,}(?!.*`)|~{3,})/;
// Lists and quotes nest by recursion; deeper than this, the lines are plain text.
const MD_MAX_DEPTH = 32;
const MD_RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const MD_QUOTE = /^ {0,3}> ?/;
const MD_ITEM = /^( {0,3})(?:[-*+]|(\d{1,9})[.)])(?:[ \t]+|$)/;
const MD_TABLE_SEP = /^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/;

// Columns of leading whitespace, with a tab as four.
function mdIndent(line) {
  let cols = 0;
  for (const ch of line) {
    if (ch === " ") cols += 1;
    else if (ch === "\t") cols += 4;
    else break;
  }
  return cols;
}
// The line with up to `cols` columns of leading whitespace removed.
function mdDedent(line, cols) {
  let i = 0, seen = 0;
  while (i < line.length && seen < cols && (line[i] === " " || line[i] === "\t")) {
    seen += line[i] === "\t" ? 4 : 1;
    i += 1;
  }
  return line.slice(i);
}
function mdIsTable(line, next) {
  return line.includes("|") && next !== undefined && next.includes("|") && MD_TABLE_SEP.test(next);
}
// True when the line starts a block, which also ends the paragraph before it.
function mdStarts(line, next) {
  return MD_HEADING.test(line) || MD_FENCE.test(line) || MD_RULE.test(line) || MD_QUOTE.test(line)
    || MD_ITEM.test(line) || mdIsTable(line, next);
}
// Only a list starting at 1 ends a paragraph, so a wrapped line such as `2024. It shipped` stays prose.
function mdListStarts(line, inParagraph) {
  const m = MD_ITEM.exec(line);
  return !!m && (!inParagraph || m[2] === undefined || Number(m[2]) === 1);
}
function mdCells(line) {
  const row = line.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "");
  return row.split(/(?<!\\)\|/).map((cell) => cell.trim());
}
function mdFence(lines, i, make) {
  const open = MD_FENCE.exec(lines[i]);
  const close = new RegExp("^ {0,3}" + open[2][0] + "{" + open[2].length + ",}[ \\t]*$");
  const body = [];
  i += 1;
  while (i < lines.length && !close.test(lines[i])) {
    body.push(mdDedent(lines[i], open[1].length));
    i += 1;
  }
  return { node: make("pre", {}, make("code", { text: body.join("\n") })), next: i + 1 };
}
function mdQuote(lines, i, make, depth) {
  const body = [];
  while (i < lines.length) {
    const line = lines[i];
    if (MD_QUOTE.test(line)) body.push(line.replace(MD_QUOTE, ""));
    else if (line.trim() && body.length && body[body.length - 1].trim() && !mdStarts(line, lines[i + 1])) body.push(line);
    else break;
    i += 1;
  }
  return { node: make("blockquote", {}, mdBlocks(body, make, false, depth + 1)), next: i };
}
function mdTable(lines, i, make) {
  const head = mdCells(lines[i]);
  const width = head.length;
  const rows = [];
  i += 2;
  while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
    const cells = mdCells(lines[i]).slice(0, width);
    while (cells.length < width) cells.push("");
    rows.push(cells);
    i += 1;
  }
  const row = (cells, tag) => make("tr", {}, cells.map((cell) => make(tag, {}, mdInline(cell, make))));
  const table = make("table", {}, make("thead", {}, row(head, "th")), make("tbody", {}, rows.map((cells) => row(cells, "td"))));
  return { node: make("div", { class: "md-table" }, table), next: i };
}
function mdList(lines, i, make, depth) {
  const first = MD_ITEM.exec(lines[i]);
  const ordered = first[2] !== undefined;
  const sameKind = (line) => {
    const m = MD_ITEM.exec(line || "");
    return !!m && (m[2] !== undefined) === ordered;
  };
  const items = [];
  let loose = false;
  while (i < lines.length) {
    if (!lines[i].trim()) {
      let j = i;
      while (j < lines.length && !lines[j].trim()) j += 1;
      if (!sameKind(lines[j])) break;
      loose = true;
      i = j;
    }
    if (!sameKind(lines[i])) break;
    const marker = MD_ITEM.exec(lines[i]);
    const rest = lines[i].slice(marker[0].length);
    const width = rest ? marker[0].length : marker[0].length + 1;
    const body = [rest];
    i += 1;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) {
        let j = i;
        while (j < lines.length && !lines[j].trim()) j += 1;
        if (j >= lines.length || mdIndent(lines[j]) < width) break;
        for (; i < j; i += 1) body.push("");
        loose = true;
        continue;
      }
      if (mdIndent(line) >= width) body.push(mdDedent(line, width));
      else if (mdStarts(line, lines[i + 1])) break;
      else body.push(line);
      i += 1;
    }
    items.push(body);
  }
  const start = ordered ? Number(first[2]) : 1;
  const props = start !== 1 ? { start: String(start) } : {};
  const node = make(ordered ? "ol" : "ul", props, items.map((body) => make("li", {}, mdBlocks(body, make, !loose, depth + 1))));
  return { node, next: i };
}

// Block nodes for the lines. In a tight list item, paragraphs are left unwrapped.
function mdBlocks(lines, make, tight, depth) {
  const out = [];
  let para = [];
  const flush = () => {
    if (!para.length) return;
    const kids = mdInline(para.map((line) => line.trim()).join(" "), make);
    if (tight) out.push(...kids);
    else out.push(make("p", {}, kids));
    para = [];
  };
  const nest = depth < MD_MAX_DEPTH;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const heading = MD_HEADING.exec(line);
    let block = null;
    if (!line.trim()) {
      flush();
      i += 1;
      continue;
    }
    if (heading) block = { node: make("h" + heading[1].length, {}, mdInline(heading[2] || "", make)), next: i + 1 };
    else if (MD_FENCE.test(line)) block = mdFence(lines, i, make);
    else if (MD_RULE.test(line)) block = { node: make("hr", {}), next: i + 1 };
    else if (nest && MD_QUOTE.test(line)) block = mdQuote(lines, i, make, depth);
    else if (nest && mdListStarts(line, para.length > 0)) block = mdList(lines, i, make, depth);
    else if (mdIsTable(line, lines[i + 1])) block = mdTable(lines, i, make);
    if (block) {
      flush();
      out.push(block.node);
      i = block.next;
    } else {
      para.push(line);
      i += 1;
    }
  }
  flush();
  return out;
}

function renderMarkdown(text, make) {
  return mdBlocks(String(text || "").replace(/\r\n?/g, "\n").split("\n"), make, false, 0);
}

// Job files open with verdict lines such as `STATUS: READY` or `RISK: low`.
// The preview shows them as a strip above the rendered body.
const MD_HEADER_LINE = /^(?:[A-Z][A-Z_-]*:[ \t]*\S.*|(?:APPROVE|REJECTED|ESCALATE)[ \t]*)$/;
function splitHeader(text) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let n = 0;
  while (n < lines.length && MD_HEADER_LINE.test(lines[n])) n += 1;
  if (!n) return { header: [], body: String(text || "") };
  return { header: lines.slice(0, n).map((line) => line.trim()), body: lines.slice(n).join("\n").replace(/^\n+/, "") };
}
