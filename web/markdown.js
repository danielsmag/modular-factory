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
function mdMatching(s, i, open, close) {
  let depth = 0;
  for (let j = i; j < s.length; j += 1) {
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
      const end = opens ? mdCloser(s, i, delim) : -1;
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

function mdBlocks(lines, make) {
  const out = [];
  let para = [];
  const flush = () => {
    if (para.length) out.push(make("p", {}, mdInline(para.map((line) => line.trim()).join(" "), make)));
    para = [];
  };
  for (const line of lines) {
    const heading = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/.exec(line);
    if (!line.trim()) {
      flush();
    } else if (heading) {
      flush();
      out.push(make("h" + heading[1].length, {}, mdInline(heading[2] || "", make)));
    } else {
      para.push(line);
    }
  }
  flush();
  return out;
}

function renderMarkdown(text, make) {
  return mdBlocks(String(text || "").replace(/\r\n?/g, "\n").split("\n"), make);
}
