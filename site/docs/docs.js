"use strict";

const SVG = "http://www.w3.org/2000/svg";
const VERSION = "0.2.1";
const GITHUB = "https://github.com/danielsmag/modular-factory";

const PAGES = [
  { id: "welcome", group: "Get started", file: "index.html", title: "Welcome",
    blurb: "A software factory you install into any repository.",
    heads: [["Install", "install"], ["Why a factory", "why-a-factory"], ["One job, two commands", "one-job-two-commands"], ["The default pipeline", "the-default-pipeline"], ["Read next", "read-next"]] },
  { id: "install", group: "Get started", file: "install.html", title: "Install",
    blurb: "Install the command, put the factory in a repository, set it up, and check it.",
    heads: [["What you need", "what-you-need"], ["Install the command", "install-the-command"], ["Install into a repository", "install-into-a-repository"], ["Personal install", "personal-install"], ["Set up", "set-up"], ["Check", "check"], ["Commit", "commit"], ["More than one tool", "more-than-one-tool"], ["Upgrade", "upgrade"], ["Remove", "remove"]] },
  { id: "first-job", group: "Get started", file: "first-job.html", title: "First job",
    blurb: "Label an issue, run the factory, and answer when a job waits.",
    heads: [["Label an issue", "label-an-issue"], ["Run", "run"], ["What the orchestrator does", "what-the-orchestrator-does"], ["Answer a person", "answer-a-person"], ["Watch", "watch"], ["Permissions", "permissions"]] },
  { id: "concepts", group: "Concepts", file: "concepts.html", title: "How it works",
    blurb: "The script decides. The orchestrator starts agents. Agents write one step.",
    heads: [["Three parts", "three-parts"], ["The loop", "the-loop"], ["Where the factory lives", "where-the-factory-lives"], ["A job's folder", "a-jobs-folder"], ["What the script will not do", "what-the-script-will-not-do"]] },
  { id: "pipeline", group: "Concepts", file: "pipeline.html", title: "Pipeline",
    blurb: "Steps, edges, loop limits, who merges, and steps you add.",
    heads: [["The default graph", "the-default-graph"], ["Steps and edges", "steps-and-edges"], ["Loop limits", "loop-limits"], ["Who merges", "who-merges"], ["Optional steps", "optional-steps"], ["Adding a step", "adding-a-step"], ["Human steps", "human-steps"], ["Validation after merge", "validation-after-merge"]] },
  { id: "guards", group: "Concepts", file: "guards.html", title: "Guards",
    blurb: "The label, quoted issue text, checks, protected paths, and what is untested.",
    heads: [["The label", "the-label"], ["Issue text is data", "issue-text-is-data"], ["Limiting intake", "intake-has-no-shell"], ["Checks are a script", "checks-are-a-script"], ["Protected paths", "protected-paths"], ["Merge and replies", "merge-and-replies"], ["The dashboard", "the-dashboard"], ["What is untested", "what-is-untested"]] },
  { id: "tools", group: "Concepts", file: "tools.html", title: "Tools",
    blurb: "Claude Code, Cursor, and Codex. Same prompts, different agent files.",
    heads: [["Where each tool writes", "where-each-tool-writes"], ["Intake", "intake"], ["Models and effort", "models-and-effort"], ["One skill, or two", "one-skill-or-two"]] },
  { id: "workspace", group: "Guides", file: "workspace.html", title: "Workspace and dashboard",
    blurb: "One read-only board for several projects, and the cap on how many jobs start.",
    heads: [["Dashboard", "dashboard"], ["Add a project", "add-a-project"], ["One board", "one-board"], ["Concurrency", "concurrency"]] },
  { id: "configuration", group: "Reference", file: "configuration.html", title: "Configuration",
    blurb: "Every key in factory.toml and in a pipeline file, including checks, reviewers, models, skills, and protected paths.",
    heads: [["How values merge", "how-values-merge"], ["Project, tools and run", "project-tools-and-run"], ["Checks", "checks"], ["Steps", "steps"], ["Loop limits", "loop-limits"], ["Who merges", "who-merges"], ["Optional steps", "optional-steps"], ["Adding a step", "adding-a-step"], ["Human steps", "human-steps"], ["Validation", "validation"], ["Reviewers and surfaces", "reviewers-and-surfaces"], ["Models and effort", "models-and-effort"], ["Project skills", "project-skills"], ["Tracker: a Jira board", "tracker-a-jira-board"], ["GitHub issues and a Jira board together", "tracker-both"], ["Labels, paths and ports", "labels-paths-and-ports"]] },
  { id: "cli", group: "Reference", file: "cli.html", title: "CLI",
    blurb: "mfactory commands for install, configuration, jobs, the dashboard, and the workspace.",
    heads: [["Install and maintain", "install-and-maintain"], ["See the configuration", "see-the-configuration"], ["Run jobs", "run-jobs"], ["Dashboard", "dashboard"], ["Workspace", "workspace"]] },
  { id: "changelog", group: "Reference", file: "changelog.html", title: "Changelog",
    blurb: "What changed in 0.2.1, 0.2.0, and 0.1.0.",
    heads: [["0.2.1", "v0-2-1"], ["0.2.0", "v0-2-0"], ["0.1.0", "v0-1-0"], ["Known limits", "known-limits"]] },
];

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
  return node;
}
function svg(path) {
  const node = document.createElementNS(SVG, "svg");
  node.setAttribute("viewBox", "0 0 24 24");
  node.setAttribute("fill", "none");
  node.setAttribute("stroke", "currentColor");
  node.setAttribute("stroke-width", "2");
  node.setAttribute("aria-hidden", "true");
  const d = document.createElementNS(SVG, "path");
  d.setAttribute("d", path);
  node.append(d);
  return node;
}
function logo() {
  const node = svg("M3 21V10l6 4V10l6 4V4h6v17z");
  node.setAttribute("stroke", "#ff4a26");
  return node;
}
function headingText(heading) {
  return [...heading.childNodes].filter((node) => node.nodeType === 3).map((node) => node.textContent).join("").trim();
}

function renderHeader() {
  const input = el("input", { id: "q", type: "search", placeholder: "Search docs...", autocomplete: "off", spellcheck: "false" });
  const hits = el("div", { id: "hits", class: "hits", hidden: true, role: "listbox" });
  const search = el("label", { class: "search" },
    svg("M16.5 16.5 21 21M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14z"),
    el("span", { class: "sr", text: "Search docs" }),
    input,
    el("kbd", { id: "hotkey", text: "Ctrl K" }),
    hits);
  const menu = el("button", { class: "menu", type: "button", "aria-label": "Open menu", "aria-expanded": "false" },
    svg("M4 7h16M4 12h16M4 17h16"));
  return el("header", { class: "top" },
    menu,
    el("a", { class: "brand", href: "index.html" }, logo(), "factory", el("span", { class: "brand-sub", text: "docs" })),
    el("nav", { class: "nav", "aria-label": "Site" },
      el("a", { href: "index.html", "aria-current": "page", text: "Docs" }),
      el("a", { href: "../index.html", text: "Dashboard" })),
    el("div", { class: "tools" },
      search,
      el("a", { class: "ver", href: "changelog.html", text: "v" + VERSION }),
      el("a", { class: "cta", href: GITHUB, target: "_blank", rel: "noopener noreferrer", text: "GITHUB" })));
}

function renderSide(current) {
  const nav = el("nav", { class: "side", id: "side", "aria-label": "Documentation" });
  let group = "";
  for (const page of PAGES) {
    if (page.group !== group) {
      group = page.group;
      nav.append(el("div", { class: "group", text: group }));
    }
    const link = el("a", { href: page.file, text: page.title });
    if (page.id === current) link.setAttribute("aria-current", "page");
    nav.append(link);
  }
  return nav;
}

function renderPager(index) {
  const pager = el("nav", { class: "pager", "aria-label": "Pages" });
  const prev = PAGES[index - 1];
  const next = PAGES[index + 1];
  pager.append(prev
    ? el("a", { href: prev.file }, el("span", { text: "Previous" }), el("b", { text: prev.title }))
    : el("span"));
  pager.append(next
    ? el("a", { class: "next", href: next.file }, el("span", { text: "Next" }), el("b", { text: next.title }))
    : el("span"));
  return pager;
}

function fillToc(article) {
  const nav = document.getElementById("toc-nav");
  const heads = [...article.querySelectorAll("h2, h3")];
  if (!heads.length) {
    document.getElementById("toc").hidden = true;
    return;
  }
  for (const heading of heads) {
    if (!heading.id) heading.id = headingText(heading).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    nav.append(el("a", {
      href: "#" + heading.id,
      class: heading.tagName === "H3" ? "sub" : "",
      text: headingText(heading),
    }));
    heading.append(el("a", { class: "anchor", href: "#" + heading.id, text: "#", "aria-label": "Link to this section" }));
  }
  const links = [...nav.querySelectorAll("a")];
  if (links[0]) links[0].classList.add("on");
  const obs = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      for (const link of links) link.classList.toggle("on", link.getAttribute("href") === "#" + entry.target.id);
    }
  }, { rootMargin: "-80px 0px -65% 0px", threshold: 0 });
  for (const heading of heads) obs.observe(heading);
}

function enhanceCode(article) {
  for (const pre of article.querySelectorAll("pre")) {
    const code = pre.querySelector("code") || pre;
    paint(code, pre.dataset.lang || "");
    const wrap = el("div", { class: "codeblock" });
    const button = el("button", { type: "button", text: "copy" });
    button.addEventListener("click", () => copyText(pre.innerText, button));
    const bar = el("div", { class: "codebar" }, el("span", { text: pre.dataset.lang || "" }), button);
    pre.parentNode.insertBefore(wrap, pre);
    wrap.append(bar, pre);
  }
}
function paint(code, lang) {
  if (lang !== "toml" && lang !== "bash" && lang !== "yaml") return;
  const src = code.textContent;
  const lines = src.split("\n");
  const nodes = [];
  const push = (kind, text) => {
    if (!text) return;
    nodes.push(kind ? el("span", { class: "tok " + kind, text }) : document.createTextNode(text));
  };
  lines.forEach((line, index) => {
    if (lang === "toml") tomlLine(line, push);
    else if (lang === "yaml") yamlLine(line, push);
    else bashLine(line, push);
    if (index < lines.length - 1) push("", "\n");
  });
  code.replaceChildren(...nodes);
}
function quoted(text, push) {
  let i = 0;
  while (i < text.length) {
    if (text[i] === "#") { push("com", text.slice(i)); return; }
    if (text[i] === '"' || text[i] === "'") {
      const quote = text[i];
      let j = i + 1;
      while (j < text.length && text[j] !== quote) {
        if (text[j] === "\\" && quote === '"') j += 2;
        else j += 1;
      }
      if (j < text.length) j += 1;
      push("str", text.slice(i, j));
      i = j;
      continue;
    }
    let j = i;
    while (j < text.length && text[j] !== "#" && text[j] !== '"' && text[j] !== "'") j += 1;
    push("", text.slice(i, j));
    i = j;
  }
}
function tomlLine(line, push) {
  const trimmed = line.trimStart();
  if (trimmed.startsWith("#")) { push("com", line); return; }
  if (trimmed.startsWith("[")) {
    const end = line.indexOf("]");
    if (end !== -1) {
      push("sec", line.slice(0, end + 1));
      quoted(line.slice(end + 1), push);
      return;
    }
  }
  const eq = line.indexOf("=");
  if (eq > 0 && !line.slice(0, eq).includes('"') && !line.slice(0, eq).includes("'")) {
    push("key", line.slice(0, eq));
    push("", "=");
    quoted(line.slice(eq + 1), push);
    return;
  }
  quoted(line, push);
}
function yamlLine(line, push) {
  const trimmed = line.trimStart();
  if (trimmed.startsWith("#")) { push("com", line); return; }
  const found = line.match(/^(\s*-?\s*)([A-Za-z0-9_.-]+)(:)(.*)$/);
  if (found) {
    push("", found[1]);
    push("key", found[2]);
    push("", found[3]);
    quoted(found[4], push);
    return;
  }
  quoted(line, push);
}
function bashLine(line, push) {
  if (line.trimStart().startsWith("#")) { push("com", line); return; }
  let i = 0;
  let word = true;
  while (i < line.length) {
    if (line[i] === "#") { push("com", line.slice(i)); return; }
    if (line[i] === '"' || line[i] === "'") {
      const quote = line[i];
      let j = i + 1;
      while (j < line.length && line[j] !== quote) j += 1;
      if (j < line.length) j += 1;
      push("str", line.slice(i, j));
      i = j;
      word = false;
      continue;
    }
    if (line.startsWith("--", i)) {
      let j = i;
      while (j < line.length && !/\s/.test(line[j])) j += 1;
      push("flag", line.slice(i, j));
      i = j;
      word = false;
      continue;
    }
    if (word && /\S/.test(line[i])) {
      let j = i;
      while (j < line.length && !/\s/.test(line[j])) j += 1;
      push("cmd", line.slice(i, j));
      i = j;
      word = false;
      continue;
    }
    push("", line[i]);
    if (line[i] === "|" || line[i] === "&" || line[i] === ";") word = true;
    i += 1;
  }
}
function copyText(text, button) {
  const done = () => {
    button.textContent = "copied";
    setTimeout(() => { button.textContent = "copy"; }, 1200);
  };
  const fallback = () => {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.append(area);
    area.select();
    try { document.execCommand("copy"); done(); } catch { button.textContent = "copy"; }
    area.remove();
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done).catch(fallback);
  else fallback();
}

function bindSearch() {
  const input = document.getElementById("q");
  const hits = document.getElementById("hits");
  let items = [];
  let selected = 0;
  const show = (query) => {
    items = find(query);
    hits.replaceChildren();
    if (!query.trim()) { hits.hidden = true; return; }
    hits.hidden = false;
    if (!items.length) {
      hits.append(el("div", { class: "empty", text: "No pages match." }));
      return;
    }
    items.forEach((item, i) => {
      const link = el("a", { href: item.href, role: "option", "aria-selected": i === selected },
        el("span", { class: "hit-title", text: item.title }),
        el("span", { class: "where", text: item.where }));
      hits.append(link);
    });
  };
  const move = (delta) => {
    const links = [...hits.querySelectorAll("a")];
    if (!links.length) return;
    selected = (selected + delta + links.length) % links.length;
    links.forEach((link, i) => link.setAttribute("aria-selected", i === selected));
    links[selected].scrollIntoView({ block: "nearest" });
  };
  input.addEventListener("input", () => { selected = 0; show(input.value); });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") { event.preventDefault(); move(1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); move(-1); }
    else if (event.key === "Enter") {
      const link = hits.querySelector('[aria-selected="true"]');
      if (link) { event.preventDefault(); location.href = link.getAttribute("href"); }
    } else if (event.key === "Escape") {
      input.value = "";
      hits.hidden = true;
      input.blur();
    }
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".search")) hits.hidden = true;
  });
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      input.focus();
      input.select();
    } else if (event.key === "/" && document.activeElement !== input && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      input.focus();
    }
  });
  if (/Mac|iPhone|iPad/.test(navigator.platform)) document.getElementById("hotkey").textContent = "⌘ K";
}
function find(query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  const out = [];
  for (const page of PAGES) {
    const title = page.title.toLowerCase();
    const blurb = page.blurb.toLowerCase();
    if (title.includes(needle) || blurb.includes(needle) || page.group.toLowerCase().includes(needle)) {
      out.push({ href: page.file, title: page.title, where: page.group, score: title.includes(needle) ? 0 : 1 });
    }
    for (const [label, hash] of page.heads) {
      if (label.toLowerCase().includes(needle)) {
        out.push({ href: page.file + "#" + hash, title: label, where: page.group + " · " + page.title, score: 2 });
      }
    }
  }
  out.sort((a, b) => a.score - b.score || a.title.localeCompare(b.title));
  return out.slice(0, 12);
}

function bindMenu() {
  const button = document.querySelector(".menu");
  const side = document.getElementById("side");
  const scrim = el("button", { class: "scrim", type: "button", hidden: true, "aria-label": "Close menu" });
  document.body.append(scrim);
  const set = (open) => {
    side.classList.toggle("open", open);
    scrim.hidden = !open;
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.setAttribute("aria-label", open ? "Close menu" : "Open menu");
  };
  button.addEventListener("click", () => set(!side.classList.contains("open")));
  scrim.addEventListener("click", () => set(false));
  side.addEventListener("click", (event) => { if (event.target.closest("a")) set(false); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") set(false); });
}

function boot() {
  const article = document.getElementById("doc");
  const id = document.body.dataset.page;
  const index = PAGES.findIndex((page) => page.id === id);
  const page = PAGES[index];
  if (!article || !page) return;
  document.title = page.title + " · factory";
  const first = PAGES.find((item) => item.group === page.group);
  article.prepend(el("p", { class: "crumb" },
    el("a", { href: first.file, text: page.group }),
    el("span", { text: "/" }),
    page.title));
  const toc = el("aside", { class: "toc", id: "toc", "aria-label": "On this page" },
    el("p", { class: "toc-label", text: "On this page" }),
    el("nav", { id: "toc-nav" }));
  const main = el("div", { class: "main" },
    article,
    renderPager(index),
    el("footer", { class: "foot" },
      el("span", { text: "factory " + VERSION + " · GPLv3" }),
      el("span", {}, el("a", { href: GITHUB, text: "github.com/danielsmag/modular-factory" }))));
  document.body.append(renderHeader(), el("div", { class: "shell" }, renderSide(id), main, toc));
  fillToc(article);
  enhanceCode(article);
  bindSearch();
  bindMenu();
  if (location.hash) {
    const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (target) target.scrollIntoView();
  }
}

document.addEventListener("DOMContentLoaded", boot);
