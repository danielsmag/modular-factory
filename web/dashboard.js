"use strict";
// Everything shown here may come from a GitHub issue, so no markup is ever built
// from strings: nodes are created one by one and text goes in as text.
const SVG = "http://www.w3.org/2000/svg";
const REFRESH_MS = 5000;
const QUEUE_MS = 15000;
// The Board's Jira and GitHub blocks: how old an answer may get, and how often to look for old ones.
const FEED_MS = 60000;
const FEED_TICK_MS = 15000;
const BLOCKS_KEY = "mfactory.board.collapsed";
const GH_FILTER_KEY = "mfactory.github.filter";
const GH_FILTER_NAMES = ["all", "mine", "outside"];
const RUNNING = new Set(["agent", "checks", "review", "merge", "verify"]);
const CHOICE_KEY = "mfactory.workspace.selected";
const FOOT_NOTE = "read-only · approvals and feedback go through /mfactory or an issue comment";
const FALLBACK = [
  ["intake", ["intake", "ask-author"]],
  ["spec", ["spec"]],
  ["build", ["build", "gate", "debug"]],
  ["review", ["review"]],
  ["approve", ["approve", "merge", "verify"]],
  ["needs-you", ["spec-approval", "escalated"]],
  ["done", ["done", "rejected", "failed"]],
];
function queryProject() {
  try {
    if (typeof location === "undefined") return "";
    return new URLSearchParams(location.search).get("project") || "";
  } catch (error) {
    return "";
  }
}
function readChoice() {
  const chosen = queryProject();
  if (chosen) return chosen;
  try {
    if (typeof localStorage === "undefined") return "all";
    return localStorage.getItem(CHOICE_KEY) || "all";
  } catch (error) {
    return "all";
  }
}
function writeChoice(id) {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(CHOICE_KEY, id);
  } catch (error) {}
}
function projectHref(href, id) {
  try {
    const url = new URL(href);
    if (id === "all") url.searchParams.delete("project");
    else url.searchParams.set("project", id);
    return url.pathname + url.search + url.hash;
  } catch (error) {
    return href;
  }
}
function writeUrl(id) {
  try {
    if (typeof history === "undefined" || typeof location === "undefined") return;
    const next = projectHref(location.href, id);
    const current = location.pathname + location.search + location.hash;
    if (next !== current) history.replaceState(history.state, "", next);
  } catch (error) {}
}
function chooseProject(id) {
  state.selected = id;
  writeChoice(id);
  writeUrl(id);
  render();
  refreshNow();
  refreshFeeds(false);
}
// View settings kept in this browser, as JSON. Storage can be missing or refuse, so every read has a fallback.
const prefs = {
  get(key, fallback) {
    try {
      if (typeof localStorage === "undefined") return fallback;
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (error) {
      return fallback;
    }
  },
  set(key, value) {
    try {
      if (typeof localStorage !== "undefined") localStorage.setItem(key, JSON.stringify(value));
    } catch (error) {}
  },
};
function storedFolds() {
  const found = prefs.get(BLOCKS_KEY, []);
  return Array.isArray(found) ? found.filter((id) => typeof id === "string") : [];
}
function storedFilter() {
  const found = prefs.get(GH_FILTER_KEY, "all");
  return GH_FILTER_NAMES.indexOf(found) >= 0 ? found : "all";
}
const SPLIT_KEY = "factory-split";
const SPLIT_MIN = 140;
const SPLIT_DEFAULT = 220;
function readSplit() {
  try {
    const n = Number(localStorage.getItem(SPLIT_KEY));
    if (n >= SPLIT_MIN && n <= 720) return n;
  } catch (error) {}
  return SPLIT_DEFAULT;
}
function writeSplit(px) {
  state.split = px;
  try { localStorage.setItem(SPLIT_KEY, String(px)); } catch (error) {}
}
const state = {
  index: null, graph: null, config: null, error: null, query: "", range: "30", file: null, source: "all", drawer: null,
  workspace: false, projects: [], selected: readChoice(), queueRows: [], queueErrors: [], queueStale: false,
  split: readSplit(), now: {}, nowSeq: 0, nowSeen: {}, raw: false, planUsage: null, unit: null,
  feeds: { jira: {}, github: {} }, jiraPicker: null,
  ghFilter: storedFilter(), collapsed: new Set(storedFolds()),
  memo: new Map(), jobsStamp: 0, queueStamp: 0,
  pipelines: null, jobSort: prefs.get("mfactory.jobs.sort", { key: "started", dir: -1 }),
  jobFilter: prefs.get("mfactory.jobs.filter", "all"), jobPipe: "all", jobTab: prefs.get("mfactory.job.tab", "graph"),
  jobNode: null, dagZoom: {}, pipeView: prefs.get("mfactory.pipeView", "default"),
  cfgPipe: prefs.get("mfactory.cfgPipe", "default"), cfgTab: "dag",
};
if (queryProject()) writeChoice(state.selected);

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
function svg(tag, attrs, ...kids) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "text") node.textContent = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  for (const kid of kids.flat(Infinity)) if (kid) node.append(kid);
  return node;
}
// An issue as people write it: a GitHub number gets "#", a named key such as PROJ-123 is shown as it is.
function issueRef(key) {
  const text = key == null ? "" : String(key);
  return /^\d+$/.test(text) ? "#" + text : text;
}
// A link the page may follow: a web address, or a server on this machine such as a local Jira.
const WEB_URL = /^(?:https:\/\/\S+|http:\/\/(?:127\.0\.0\.1|localhost)(?::\d+)?(?:\/\S*)?)$/;
function link(url, text) {
  // Only those become links; anything else is shown as plain text.
  return WEB_URL.test(url || "") ? el("a", { class: "ext", href: url, target: "_blank", rel: "noopener noreferrer", text }) : el("span", { class: "mono", text });
}
// The job's pull requests, oldest first: those merged before a follow-up, then the current one.
function jobPrs(job) {
  const earlier = (job.prs || []).filter((p) => p && p.number).map((p) => ({ number: p.number, url: p.url, earlier: true }));
  return job.pr ? earlier.concat([{ number: job.pr, url: job.pr_url, earlier: false }]) : earlier;
}
function prLinks(job, prefix) {
  return jobPrs(job).map((p) => link(p.url, prefix + p.number + (p.earlier ? " (merged)" : "")));
}
function icon(path) {
  const node = svg("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, "aria-hidden": "true" });
  node.append(svg("path", { d: path }));
  return node;
}

// ---- data helpers ----
const time = (stamp) => (stamp ? new Date(stamp).getTime() : null);
function ago(stamp) {
  const t = time(stamp);
  if (!t) return "-";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  if (s < 129600) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
function span(seconds) {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return "-";
  if (seconds < 60) return Math.round(seconds) + "s";
  if (seconds < 3600) return Math.round(seconds / 60) + "m";
  if (seconds < 172800) return (seconds / 3600).toFixed(1).replace(/\.0$/, "") + "h";
  return Math.round(seconds / 86400) + "d";
}
const money = (n) => "$" + Number(n).toFixed(2);
function graphOf(job) {
  if (job && job.graph) return job.graph;
  if (job && !state.workspace && state.pipelines) return pipelineGraphFor(null, job.pipeline);
  return state.graph;
}
function stepOf(name, job) {
  const graph = graphOf(job);
  if (!graph || !Array.isArray(graph.steps)) return null;
  return graph.steps.find((s) => s.name === name) || null;
}
function isEnd(name, job) {
  const graph = graphOf(job);
  return !!(graph && Array.isArray(graph.end_states) && graph.end_states.includes(name));
}
function kindOf(job) {
  if (job.step === "done") return "done";
  if (job.step === "rejected" || job.step === "failed") return "bad";
  if (job.waiting) return "wait";
  return "run";
}
function chip(job) {
  return el("span", { class: "chip " + kindOf(job), text: job.step });
}
function matches(job) {
  const q = state.query.trim().toLowerCase();
  if (!q) return true;
  return [job.project, job.projectName, job.id, job.title, issueRef(job.issue), job.step, job.author, ...jobPrs(job).map((p) => "#" + p.number)].some((v) => String(v || "").toLowerCase().includes(q));
}
function visibleProjects() {
  if (!state.workspace) return [];
  return state.projects.filter((project) => state.selected === "all" || state.selected === project.id);
}
function projectLabel(project) {
  const name = project.name || project.id;
  const shared = state.projects.filter((item) => (item.name || item.id) === name).length > 1;
  return shared ? project.id : name;
}
function taggedJob(project, job) {
  const copy = Object.assign({}, job);
  copy.project = project.id;
  copy.projectName = projectLabel(project);
  copy.graph = pipelineGraphFor(project, job.pipeline);
  return copy;
}
function jobs() {
  if (state.workspace) {
    const list = [];
    visibleProjects().forEach((project) => {
      (project.jobs || []).forEach((job) => list.push(taggedJob(project, job)));
    });
    list.sort((a, b) => {
      const name = String(a.projectName || "").localeCompare(String(b.projectName || ""));
      if (name) return name;
      return a.id < b.id ? 1 : a.id > b.id ? -1 : 0;
    });
    return list;
  }
  return state.index ? state.index.jobs.slice().sort((a, b) => (a.id < b.id ? 1 : -1)) : [];
}
function queueMatches(row) {
  const q = state.query.trim().toLowerCase();
  if (!q) return true;
  const labels = Array.isArray(row.labels) ? row.labels : [];
  return [row.project, row.projectName, row.title, row.note, issueRef(row.issue), row.action, row.job, row.step, row.author, ...labels].some((value) => String(value || "").toLowerCase().includes(q));
}
function queueCards(rows, jobList, actions = ["new", "hold"]) {
  const ids = new Set(jobList.map((job) => job.id));
  const issues = new Set(jobList.map((job) => String(job.issue)));
  const seen = new Set();
  const out = [];
  for (const row of rows) {
    if (!actions.includes(row.action)) continue;
    if (row.job && row.job !== "-" && ids.has(row.job)) continue;
    if (issues.has(String(row.issue))) continue;
    const key = String(row.issue);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(row);
  }
  return out;
}
function commands(job) {
  const step = stepOf(job.step, job);
  if (!step || !job.waiting) return [];
  const hint = { REJECT: " <why>", ANSWER: " <your answer>", APPROVE: "" };
  return (step.edges || []).map((e) => "/mfactory " + e.outcome.toLowerCase() + " " + job.issue + (e.outcome in hint ? hint[e.outcome] : " <what to change>"));
}

// ---- board ----
function boardTotals(all) {
  const running = all.filter((j) => { const s = stepOf(j.step, j); return s && RUNNING.has(s.type) && !j.waiting; }).length;
  const cost = all.reduce((sum, j) => sum + (Number(j.cost_usd) || 0), 0);
  return { running, cost };
}
function columnsFor(graph, allJobs) {
  const steps = new Map((graph.steps || []).map((s) => [s.name, s]));
  const ends = new Set(graph.end_states || []);
  const present = new Set(allJobs.map((j) => j.step).filter((name) => name));
  const cols = [];
  const columnOf = (name) => cols.find((col) => col.steps.includes(name));
  const workColumn = () => {
    for (let i = cols.length - 1; i >= 0; i--) if (cols[i].id !== "needs-you") return cols[i];
    return null;
  };
  for (const name of order(graph)) {
    if (ends.has(name)) continue;
    const step = steps.get(name);
    if (!step) continue;
    if (step.enabled === false && !present.has(name)) continue;
    if (step.type === "human") {
      if (step.who === "author") {
        const target = (step.edges || []).map((edge) => edge.to).find((to) => columnOf(to));
        if (target) { columnOf(target).steps.push(name); continue; }
      }
      let needs = cols.find((col) => col.id === "needs-you");
      if (!needs) {
        needs = { id: "needs-you", label: "Needs you", cls: "human", steps: [] };
        cols.push(needs);
      }
      needs.steps.push(name);
      continue;
    }
    if ((step.type === "checks" || step.type === "merge" || step.type === "verify") && workColumn()) {
      workColumn().steps.push(name);
      continue;
    }
    cols.push({ id: name, label: name, cls: "", steps: [name] });
  }
  const needs = cols.find((col) => col.id === "needs-you");
  const ordered = cols.filter((col) => col.id !== "needs-you");
  if (needs) ordered.push(needs);
  const covered = new Set(ordered.flatMap((col) => col.steps).concat(graph.end_states || []));
  const stray = [...present].filter((name) => !covered.has(name));
  if (stray.length) ordered.push({ id: "other", label: "Other", cls: "", steps: stray });
  if ((graph.end_states || []).length) ordered.push({ id: "done", label: "Done", cls: "done", steps: graph.end_states.slice() });
  return ordered;
}
function fallbackColumns(jobList) {
  const present = new Set(jobList.map((job) => job.step).filter((name) => name));
  const known = new Set(FALLBACK.flatMap((row) => row[1]));
  const cols = [];
  FALLBACK.forEach((row) => {
    const mine = row[1].filter((name) => present.has(name));
    if (!mine.length) return;
    const id = row[0];
    cols.push({
      id,
      label: id === "needs-you" ? "Needs you" : id === "done" ? "Done" : id,
      cls: id === "needs-you" ? "human" : id === "done" ? "done" : "",
      steps: mine,
    });
  });
  const stray = [...present].filter((name) => !known.has(name));
  if (stray.length) {
    const other = { id: "other", label: "Other", cls: "", steps: stray };
    const doneAt = cols.findIndex((col) => col.id === "done");
    if (doneAt >= 0) cols.splice(doneAt, 0, other);
    else cols.push(other);
  }
  return cols;
}
function columnsOf(project) {
  const list = project.jobs || [];
  const graph = project.graph;
  if (graph && Array.isArray(graph.steps) && Array.isArray(graph.end_states)) {
    try { return columnsFor(graph, list); } catch (error) { /* a broken graph still shows the jobs */ }
  }
  return fallbackColumns(list);
}
function unionColumns(projects, allJobs) {
  const merged = [];
  const claimed = new Set();
  function add(col) {
    const steps = [];
    (col.steps || []).forEach((name) => {
      if (!name || claimed.has(name)) return;
      claimed.add(name);
      steps.push(name);
    });
    if (!steps.length) return;
    const existing = merged.find((item) => item.id === col.id);
    if (existing) {
      existing.steps.push(...steps);
      return;
    }
    const entry = { id: col.id, label: col.label, cls: col.cls || "", steps };
    const needsAt = merged.findIndex((item) => item.id === "needs-you");
    const doneAt = merged.findIndex((item) => item.id === "done");
    let at = merged.length;
    if (col.id !== "done" && col.id !== "needs-you") {
      if (needsAt >= 0) at = Math.min(at, needsAt);
      if (doneAt >= 0) at = Math.min(at, doneAt);
    } else if (col.id === "needs-you" && doneAt >= 0) {
      at = doneAt;
    }
    merged.splice(at, 0, entry);
  }
  projects.forEach((project) => columnsOf(project).forEach(add));
  const stray = [];
  allJobs.forEach((job) => {
    if (job.step && !claimed.has(job.step) && stray.indexOf(job.step) < 0) stray.push(job.step);
  });
  if (stray.length) add({ id: "other", label: "Other", cls: "", steps: stray });
  return merged;
}
function queueFor(projects, actions) {
  const rows = [];
  projects.forEach((project) => {
    const mine = state.queueRows.filter((row) => row.project === project.id);
    queueCards(mine, project.jobs || [], actions).forEach((row) => {
      const copy = Object.assign({}, row);
      copy.projectName = projectLabel(project);
      rows.push(copy);
    });
  });
  return rows;
}
function boardColumns(all) {
  if (!state.workspace) return columnsFor(state.graph, all);
  const projects = visibleProjects();
  const cols = projects.length === 1
    ? columnsOf(projects[0]).map((col) => Object.assign({}, col, { steps: col.steps.slice() }))
    : unionColumns(projects, all);
  const queued = queueFor(projects);
  if (queued.length) cols.unshift({ id: "queue", label: "Queue", cls: "", steps: [], queue: queued });
  // Cards in [tracker.jira.columns].watch statuses: shown as coming up, never built.
  const watched = queueFor(projects, ["watch"]);
  if (watched.length) cols.unshift({ id: "watching", label: "Watching", cls: "", steps: [], queue: watched });
  return cols;
}
function columnWho(col) {
  if (col.id === "queue") return "not a job yet";
  if (col.id === "watching") return "never built";
  if (col.id === "needs-you" || col.id === "other" || col.id === "done") return col.steps.join(" · ");
  return col.steps.slice(1).join(" · ");
}
function jobNum(id) {
  const text = String(id || "");
  const cut = text.indexOf("-");
  return cut > 0 ? text.slice(0, cut) + " · " + text.slice(cut + 1) : text;
}
function elapsed(job) {
  const start = time(job.created_at);
  if (!start) return "-";
  return span(((time(job.finished_at) || Date.now()) - start) / 1000);
}
function ageText(kind, stamp) {
  if (kind === "span") {
    const start = time(stamp);
    if (!start) return "-";
    return span((Date.now() - start) / 1000);
  }
  if (kind === "since") {
    const label = ago(stamp);
    return label === "-" ? "since -" : "since " + label;
  }
  return ago(stamp);
}
function ageNode(stamp, kind) {
  const text = ageText(kind, stamp);
  if (!time(stamp)) return el("span", { text });
  return el("span", { "data-age": stamp, "data-age-kind": kind, text });
}
function elapsedNode(job) {
  if (!time(job.created_at)) return el("span", { text: "-" });
  if (time(job.finished_at)) return el("span", { text: elapsed(job) });
  return ageNode(job.created_at, "span");
}
function paintAges() {
  document.querySelectorAll("[data-age]").forEach((node) => {
    const text = ageText(node.getAttribute("data-age-kind") || "ago", node.getAttribute("data-age"));
    if (node.textContent !== text) node.textContent = text;
  });
}
function waitingAudience(job) {
  const who = job.waiting && job.waiting.who;
  if (who === "author") return "the author (" + person(job.author, job.author_name, job.project, job.issue, job.tracker) + ")";
  if (who === "operator") return job.waiting.operator ? "the operator (" + person(job.waiting.operator, null, job.project, job.issue, job.tracker) + ")" : "the operator";
  if (Array.isArray(who)) {
    const names = who.map((login) => person(login, null, job.project, job.issue, job.tracker));
    if (names.length <= 1) return names[0] || "a maintainer";
    if (names.length === 2) return names[0] + " or " + names[1];
    return names.slice(0, -1).join(", ") + ", or " + names[names.length - 1];
  }
  return "a maintainer";
}
function flagOf(job) {
  const who = job.waiting && job.waiting.who;
  if (who === "author") return "asked author";
  if (who === "operator" || who === "maintainer") return "your call";
  if (Array.isArray(who)) return who.length === 1 ? "asked " + person(who[0], null, job.project, job.issue, job.tracker) : "asked " + who.length + " people";
  if (job.step === "rejected") return "rejected";
  return "";
}
function riskClass(risk) {
  const name = String(risk || "").toLowerCase();
  if (name === "high") return "hot";
  if (name === "medium") return "warn";
  return "";
}
function chipVerdict(line, prefix) {
  const match = String(line || "").match(new RegExp("^" + prefix + ":\\s*(PASS|FAIL|CHANGES)\\b"));
  if (match) return match[1];
  return line ? "bad" : "pending";
}
function shortReview(name) {
  if (name === "security") return "sec";
  return name.length > 6 ? name.slice(0, 4) : name;
}
function roundRows(job) {
  const files = job.files || [];
  let max = job.round || 0;
  files.forEach((file) => {
    const match = String(file.path).match(/^round-(\d+)\//);
    if (match) max = Math.max(max, Number(match[1]));
  });
  const rows = [];
  for (let n = 1; n <= max; n++) {
    const checks = files.find((file) => file.path === "round-" + n + "/checks.md");
    const gate = checks ? chipVerdict(checks.first_line, "GATE") : "pending";
    const reviews = files.filter((file) => new RegExp("^round-" + n + "/review-[^/]+\\.md$").test(file.path)).map((file) => ({
      name: file.path.split("/").pop().replace(/^review-|\.md$/g, ""),
      v: chipVerdict(file.first_line, "VERDICT"),
    })).sort((a, b) => (a.name < b.name ? -1 : 1));
    rows.push({ n, gate, reviews: gate === "PASS" ? reviews : [] });
  }
  return rows;
}
function kchip(label, verdict, waiting, gate) {
  const known = verdict === "PASS" || verdict === "FAIL" || verdict === "CHANGES";
  const cls = "kchip" + (gate ? " gate" : "") + (known ? " " + verdict : verdict === "bad" ? " bad" : waiting ? " wait" : "");
  return el("span", { class: cls, title: label + " " + verdict, text: label });
}
function roundLine(job, row) {
  const current = row.n === job.round;
  const chips = [kchip("gate", row.gate, current && job.step === "gate", true)];
  if (row.gate === "PASS") row.reviews.forEach((review) => chips.push(kchip(shortReview(review.name), review.v, current && job.step === "review", false)));
  return el("div", { class: "round" }, el("span", { class: "r", text: "R" + row.n }), chips);
}
function cardHint(job) {
  if (!job.waiting) return "";
  if (job.reason) return String(job.reason).split("\n")[0].slice(0, 180);
  const lines = commands(job);
  return lines.length ? lines[0] : "";
}
function sameDrawer(job) {
  if (!state.drawer || state.drawer.kind === "issue" || state.drawer.id !== job.id) return false;
  if (!state.workspace) return true;
  return state.drawer.project === (job.project || null);
}
function cardNum(job) {
  const num = jobNum(job.id);
  if (state.workspace && state.selected === "all" && job.projectName) return job.projectName + " · " + num;
  return num;
}
function cardClass(job) {
  const parts = ["kcard"];
  if (sameDrawer(job)) parts.push("on");
  if (job.step === "rejected") parts.push("rejected");
  const step = stepOf(job.step, job);
  if (isEnd(job.step, job)) parts.push("done");
  else if (job.waiting) parts.push("human");
  else if (step && RUNNING.has(step.type)) parts.push("live");
  return parts.join(" ");
}
function jobCard(job) {
  const flag = flagOf(job);
  const hint = cardHint(job);
  const rounds = roundRows(job);
  const label = (job.title || job.id) + (state.workspace && state.selected === "all" && job.projectName ? ", " + job.projectName : "");
  return el("article", {
    class: cardClass(job), tabindex: 0, role: "button", "aria-label": label,
    "data-focus": "card:" + (job.project || "") + ":" + job.id,
    onclick: (event) => { if (event.target.closest("a")) return; openDrawer(job); },
    onkeydown: (event) => {
      if (event.target.closest("a")) return;
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openDrawer(job); }
    },
  },
    el("div", { class: "card-top" },
      cardClass(job).includes("live") ? el("span", { class: "pulse", "aria-hidden": "true" }) : null,
      el("span", { class: "num", text: cardNum(job) }),
      flag ? el("span", { class: "flag", text: flag }) : null),
    el("p", { class: "feature", text: job.title || job.id }),
    el("div", { class: "links" },
      link(job.issue_url, "issue " + issueRef(job.issue)),
      prLinks(job, "PR #")),
    rounds.map((row) => roundLine(job, row)),
    el("div", { class: "meta" },
      job.intake && job.intake.risk ? el("span", { class: riskClass(job.intake.risk), text: job.intake.risk }) : null,
      el("span", { text: job.cost_usd !== null && job.cost_usd !== undefined ? money(job.cost_usd) : "—" }),
      elapsedNode(job)),
    hint ? el("p", { class: "hint", text: hint }) : null);
}
const FILE_ORDER = ["issue.md", "intake.md", "spec.md", "build.md", "progress.md", "decision.md",
  "verify.md", "verify-checks.md", "verify-checks.log", "rejection.md"];
const REVIEW_ORDER = ["code", "security", "ux", "ui"];
const CURRENT = 999;
/* "intake-2.md" -> { base: "intake.md", n: 2 } when the base is a name `known` accepts. */
function archived(name, known) {
  const found = String(name).match(/^(.+)-(\d+)(\.[a-z]+)$/);
  return found && known(found[1] + found[3]) ? { base: found[1] + found[3], n: Number(found[2]) } : null;
}
function roundKind(stem, ext) {
  const log = ext === "log" ? 1 : 0;
  if (stem === "checks") return CURRENT * 2 + log;
  const old = stem.match(/^checks-(\d+)$/);
  if (old) return Number(old[1]) * 2 + log;
  if (stem.startsWith("review-")) {
    const at = REVIEW_ORDER.indexOf(stem.slice(7));
    return (10 + (at >= 0 ? at : 9)) * 10000;
  }
  const debug = stem.match(/^debug(?:-(\d+))?$/);
  if (debug) return 20 * 10000 + Number(debug[1] || 0);
  if (stem === "merge-checks") return 30 * 10000 + log;
  return 50 * 10000;
}
function fileRank(path) {
  const exact = FILE_ORDER.indexOf(path);
  if (exact >= 0) return [0, exact, CURRENT, path];
  const old = archived(path, (name) => FILE_ORDER.includes(name));
  if (old) return [0, FILE_ORDER.indexOf(old.base), old.n, path];
  const feedback = String(path).match(/^feedback-(\d+)\.md$/);
  if (feedback) return [1, Number(feedback[1]), 0, path];
  const round = String(path).match(/^round-(\d+)\/(.+)\.(md|log)$/);
  if (round) return [2, Number(round[1]), roundKind(round[2], round[3]), path];
  return [3, 0, 0, path];
}
function sortedFiles(files) {
  return files.slice().sort((a, b) => {
    const left = fileRank(a.path), right = fileRank(b.path);
    return left[0] - right[0] || left[1] - right[1] || left[2] - right[2] || (left[3] < right[3] ? -1 : left[3] > right[3] ? 1 : 0);
  });
}
function fileGroup(path) {
  if (String(path).startsWith("round-")) return path.split("/")[0].replace("round-", "Round ");
  if (String(path).startsWith("feedback-")) return "Feedback";
  return "Job";
}
function fileLabel(path) {
  const round = String(path).match(/^round-(\d+)\/(.+)$/);
  if (!round) {
    const old = archived(path, (name) => FILE_ORDER.includes(name));
    return old ? old.base.replace(/\.md$/, "") + " (try " + old.n + ")" : path;
  }
  const stem = round[2].replace(/\.md$/, "").replace(/\.log$/, "");
  const tried = stem.match(/^checks-(\d+)$/);
  if (tried) return "R" + round[1] + " checks" + (String(path).endsWith(".log") ? ".log" : "") + " (try " + tried[1] + ")";
  if (stem === "checks" && String(path).endsWith(".log")) return "R" + round[1] + " checks.log";
  if (stem === "checks") return "R" + round[1] + " checks";
  if (stem.startsWith("review-")) return "R" + round[1] + " " + stem.slice(7);
  return "R" + round[1] + " " + stem;
}
function tone(first) {
  const line = String(first || "");
  if (/^(VERDICT:\s*PASS|GATE:\s*PASS|APPROVE|STATUS:\s*READY|STATUS:\s*DONE|DECISION:\s*ACCEPT|STATUS:\s*ok)/.test(line)) return "good";
  if (/^(VERDICT:\s*CHANGES|DECISION:\s*NEEDS-INFO)/.test(line)) return "warn";
  if (/^(ESCALATE|REJECTED|STATUS:\s*BLOCKED|GATE:\s*FAIL|DECISION:\s*REJECT|VERDICT:\s*FAIL)/.test(line)) return "stop";
  return "";
}
function closeDrawer() {
  state.drawer = null;
  render();
}
function fileUrl(job, path) {
  const tail = String(path).split("/").map(encodeURIComponent).join("/");
  if (state.workspace && job.project) {
    return "/projects/" + encodeURIComponent(job.project) + "/jobs/" + encodeURIComponent(job.id) + "/" + tail;
  }
  return "jobs/" + encodeURIComponent(job.id) + "/" + tail;
}
function openDrawer(job) {
  if (sameDrawer(job)) return;
  const files = sortedFiles(job.files || []);
  const path = files.length ? files[0].path : null;
  state.raw = false;
  state.drawer = {
    project: job.project || null, id: job.id, path, text: path ? "Loading..." : "No files yet.", focus: true,
    tab: "files", run: 0, runPicked: false, activity: null, activitySeq: 0,
  };
  render();
  if (path) fetchDrawer(job, path);
}
function pickFile(job, path) {
  if (!sameDrawer(job)) return;
  if (state.drawer.path !== path) state.raw = false;
  state.drawer.path = path;
  state.drawer.text = "Loading...";
  state.drawer.focus = false;
  render();
  fetchDrawer(job, path);
}
async function fetchDrawer(job, path) {
  let text;
  try {
    const res = await fetch(fileUrl(job, path), { cache: "no-store" });
    text = res.ok ? await res.text() : "Could not load " + path + " (" + res.status + ").";
  } catch (error) {
    text = "Could not load " + path + ".";
  }
  if (!sameDrawer(job) || !state.drawer || state.drawer.path !== path) return;
  state.drawer.text = text;
  render();
}
function clampSplit(px, box) {
  const max = Math.max(SPLIT_MIN, box.clientWidth - 280);
  return Math.min(max, Math.max(SPLIT_MIN, Math.round(px)));
}
function applySplit(box, px) {
  const width = clampSplit(px, box);
  state.split = width;
  box.style.setProperty("--split", width + "px");
  const bar = box.querySelector(".grip");
  if (bar) bar.setAttribute("aria-valuenow", String(width));
  return width;
}
function startSplit(event) {
  if (event.button !== 0) return;
  const bar = event.currentTarget;
  const box = bar.parentElement;
  if (!box) return;
  event.preventDefault();
  const origin = event.clientX;
  const start = state.split;
  bar.classList.add("on");
  document.body.style.cursor = "col-resize";
  document.body.style.userSelect = "none";
  function move(ev) {
    applySplit(box, start + (ev.clientX - origin));
  }
  function end() {
    bar.classList.remove("on");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", end);
    document.removeEventListener("pointercancel", end);
    writeSplit(state.split);
  }
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", end);
  document.addEventListener("pointercancel", end);
}
function keySplit(event) {
  const box = event.currentTarget.parentElement;
  if (!box) return;
  let next = state.split;
  if (event.key === "ArrowLeft") next -= 20;
  else if (event.key === "ArrowRight") next += 20;
  else if (event.key === "Home") next = SPLIT_MIN;
  else if (event.key === "End") next = box.clientWidth - 280;
  else return;
  event.preventDefault();
  writeSplit(applySplit(box, next));
}
function resetSplit(event) {
  const box = event.currentTarget.parentElement;
  if (box) writeSplit(applySplit(box, SPLIT_DEFAULT));
}
function grip() {
  return el("div", {
    class: "grip", role: "separator", title: "Drag to resize", tabindex: "0", "data-focus": "drawer-grip",
    "aria-orientation": "vertical", "aria-label": "Resize the file list",
    "aria-valuemin": String(SPLIT_MIN), "aria-valuenow": String(state.split),
    onpointerdown: startSplit, onkeydown: keySplit, ondblclick: resetSplit,
  });
}
function splitStyle() {
  return "--split:" + state.split + "px";
}
function isMarkdown(path) {
  return /\.md$/i.test(path || "");
}
function fileModeButton(raw, label) {
  const on = state.raw === raw;
  return el("button", {
    type: "button", text: label, "data-focus": "file-mode:" + (raw ? "raw" : "preview"),
    "aria-pressed": on ? "true" : "false",
    onclick: () => { if (state.raw !== raw) { state.raw = raw; render(); } },
  });
}
// A job file: Markdown as a preview with a Preview/Raw toggle, anything else as plain text.
function fileView({ path, text, extra = "", focus = null }) {
  const cls = (name) => name + (extra ? " " + extra : "");
  if (!isMarkdown(path)) return el("div", { class: "fileview" }, el("pre", { class: cls("file"), tabindex: 0, "data-focus": focus, text }));
  const bar = el("div", { class: "fv-bar" },
    el("div", { class: "tabs", role: "group", "aria-label": "File view" }, fileModeButton(false, "Preview"), fileModeButton(true, "Raw")));
  if (state.raw) return el("div", { class: "fileview" }, bar, el("pre", { class: cls("file"), tabindex: 0, "data-focus": focus, text }));
  const { header, body } = splitHeader(text);
  const strip = header.length
    ? el("div", { class: "fv-head" }, header.map((line, n) => el("span", { class: n === 0 ? tone(line) || null : null, text: line })))
    : null;
  return el("div", { class: "fileview" }, bar,
    el("div", { class: cls("file md"), tabindex: 0, "data-focus": focus }, strip, renderMarkdown(body, el)));
}
function fileList(job, files) {
  const box = el("div", { class: "d-files" });
  let group = "";
  files.forEach((file) => {
    const name = fileGroup(file.path);
    if (name !== group) {
      group = name;
      box.append(el("div", { class: "d-group", text: name }));
    }
    const mark = tone(file.first_line);
    box.append(el("button", {
      type: "button", class: "d-file" + (state.drawer.path === file.path ? " on" : ""),
      "data-focus": "drawer-file:" + file.path,
      "aria-pressed": String(state.drawer.path === file.path),
      onclick: () => pickFile(job, file.path),
    }, el("i", { class: "dot" + (mark ? " " + mark : "") }), el("span", { text: fileLabel(file.path) })));
  });
  if (!files.length) box.append(el("p", { class: "kempty", text: "No files yet." }));
  return box;
}
function drawerTab(name, label) {
  const on = state.drawer && state.drawer.tab === name;
  return el("button", {
    type: "button", role: "tab", text: label, "data-focus": "drawer-tab:" + name,
    "aria-selected": String(on), "aria-pressed": on ? "true" : "false",
    onclick: () => pickDrawerTab(name),
  });
}
function pickDrawerTab(name) {
  if (!state.drawer || state.drawer.tab === name) return;
  state.drawer.tab = name;
  render();
  if (name === "activity") refreshActivity();
}
function activityUrl(job) {
  if (state.workspace && job.project) {
    return "/projects/" + encodeURIComponent(job.project) + "/jobs/" + encodeURIComponent(job.id) + "/activity";
  }
  return "jobs/" + encodeURIComponent(job.id) + "/activity";
}
function roundAttempt(run) {
  const round = run.round == null ? "R-" : "R" + run.round;
  const attempt = run.attempt == null ? "a-" : "a" + run.attempt;
  return round + " · " + attempt;
}
function durationLabel(seconds) {
  const total = Math.max(0, Math.round(Number(seconds)));
  const minutes = Math.floor(total / 60);
  const remain = total % 60;
  if (minutes >= 60) return Math.floor(minutes / 60) + "h " + (minutes % 60) + "m";
  return minutes + "m " + String(remain).padStart(2, "0") + "s";
}
function runMeta(run) {
  const bits = [];
  if (run.step) bits.push(String(run.step));
  if (run.model) bits.push(String(run.model));
  if (run.effort) bits.push(String(run.effort));
  bits.push(roundAttempt(run));
  if (run.duration_s != null) bits.push(durationLabel(run.duration_s));
  if (typeof run.cost_usd === "number") bits.push(money(run.cost_usd));
  return bits.join("  ");
}
function pickRun(index) {
  if (!state.drawer) return;
  state.drawer.run = index;
  state.drawer.runPicked = true;
  render();
}
function runList(job, runs, index) {
  const box = el("div", { class: "d-runs" });
  runs.forEach((run, at) => {
    box.append(el("button", {
      type: "button", class: "d-run" + (at === index ? " on" : ""),
      "data-focus": "drawer-run:" + at,
      "aria-pressed": String(at === index),
      onclick: () => pickRun(at),
    },
      el("span", { class: "who", text: run.agent || "agent" }),
      el("span", { class: "d-run-meta" },
        run.status === "running" ? el("i", { class: "pulse", "aria-hidden": "true" }) : null,
        el("span", { text: runMeta(run) }))));
  });
  return box;
}
const EDIT_NAMES = new Set(["write", "edit", "strreplace", "search_replace", "multiedit", "notebookedit", "delete"]);
const SHELL_NAMES = new Set(["bash", "shell"]);
function isStepEvent(event) {
  if (!event) return false;
  if (event.kind === "step") return true;
  const name = String(event.name || "").toLowerCase();
  return name === "updatecurrentstep" || name === "update_current_step";
}
function verbOf(event) {
  const name = String(event.name || "").toLowerCase();
  if (event.kind === "edit" || EDIT_NAMES.has(name)) return "Edited";
  if (name === "read") return "Read";
  if (name === "grep" || name === "glob") return name === "glob" ? "Found" : "Searched";
  if (SHELL_NAMES.has(name)) return "Ran";
  if (name === "webfetch" || name === "websearch") return "Searched";
  return "Used";
}
function shortPath(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  if (parts.length <= 2) return parts.join("/");
  return parts.slice(-2).join("/");
}
function clipLine(text, max) {
  const line = String(text || "").split("\n")[0].trim();
  if (line.length <= max) return line;
  return line.slice(0, max - 1) + "…";
}
function actionLabel(event) {
  const name = String(event.name || "").toLowerCase();
  if (event.job_file) return event.job_file;
  if ((name === "grep" || name === "glob") && event.summary) {
    const summary = String(event.summary);
    const pattern = event.path && summary.startsWith(event.path) ? summary.slice(event.path.length).trim() : summary;
    const where = event.path ? shortPath(event.path) : "";
    if (pattern && where) return clipLine(pattern, 48) + "  in " + where;
    return clipLine(pattern || where, 72);
  }
  if (event.path) return shortPath(event.path);
  const summary = clipLine(event.summary, 72);
  if (verbOf(event) === "Used" && event.name) return summary ? event.name + "  " + summary : event.name;
  return summary;
}
const EXPLORE_NAMES = new Set(["read", "grep", "glob"]);
function isExplore(event) {
  return event.kind !== "edit" && EXPLORE_NAMES.has(String(event.name || "").toLowerCase());
}
function isShell(event) {
  return SHELL_NAMES.has(String(event.name || "").toLowerCase());
}
function isEdit(event) {
  const name = String(event.name || "").toLowerCase();
  return event.kind === "edit" || EDIT_NAMES.has(name);
}
function feedOf(run) {
  const feed = [];
  (run.events || []).forEach((event) => {
    if (isStepEvent(event)) {
      feed.push({ type: "heading", text: event.summary || "Working" });
      return;
    }
    if (event.kind === "text") {
      if (event.text) feed.push({ type: "text", text: event.text });
      return;
    }
    const last = feed[feed.length - 1];
    if (isExplore(event)) {
      if (last && last.type === "explore") last.events.push(event);
      else feed.push({ type: "explore", events: [event] });
      return;
    }
    if (isShell(event)) {
      feed.push({ type: "shell", event });
      return;
    }
    if (isEdit(event)) {
      feed.push({ type: "edit", event });
      return;
    }
    feed.push({ type: "tool", event });
  });
  if (run.result) feed.push({ type: "text", text: run.result, result: true });
  return feed;
}
function runIdentity(run) {
  return [run.agent || "", run.step || "", run.round, run.attempt, run.started_at || ""].join("|");
}
function plural(count, word) {
  return count + " " + word + (count === 1 ? "" : "s");
}
function exploreLabel(events) {
  let files = 0;
  let searches = 0;
  events.forEach((event) => {
    if (String(event.name || "").toLowerCase() === "read") files += 1;
    else searches += 1;
  });
  const bits = [];
  if (files) bits.push(plural(files, "file"));
  if (searches) bits.push(plural(searches, "search"));
  return "Explored " + bits.join(", ");
}
function shellCommand(event) {
  return String(event.detail || event.summary || "");
}
function programsOf(command) {
  const names = [];
  String(command || "").split(/&&|\|\||;|\||\n/).forEach((part) => {
    const tokens = part.trim().split(/\s+/).filter((token) => token && !/^[A-Za-z_][A-Za-z0-9_]*=/.test(token));
    if (!tokens.length) return;
    const name = tokens[0].split("/").pop();
    if (name && !names.includes(name)) names.push(name);
  });
  return names;
}
function tickName(name) {
  return "`" + String(name || "") + "`";
}
function programsLabel(command) {
  const names = programsOf(command);
  if (!names.length) return "";
  const shown = names.length <= 6 ? names : names.slice(0, 6);
  const label = shown.map(tickName).join(", ");
  if (names.length <= 6) return label;
  return label + ", +" + (names.length - 6);
}
function editTarget(event) {
  return shortPath(event.path || event.summary || event.name || "") || "file";
}
function toolName(event) {
  const raw = String(event.name || "tool");
  const parts = raw.split(".");
  return parts[parts.length - 1] || raw;
}
function workKey(run) {
  return "work|" + runIdentity(run);
}
function workOpen(run) {
  const store = state.drawer && state.drawer.openSteps;
  const key = workKey(run);
  if (store && Object.prototype.hasOwnProperty.call(store, key)) return !!store[key];
  return true;
}
function toggleWork(key, open) {
  if (!state.drawer) return;
  if (!state.drawer.openSteps) state.drawer.openSteps = {};
  state.drawer.openSteps[key] = open;
  render();
}
function actionOpen(key) {
  return !!(state.drawer && state.drawer.openActions && state.drawer.openActions[key]);
}
function toggleAction(key, open) {
  if (!state.drawer) return;
  if (!state.drawer.openActions) state.drawer.openActions = {};
  state.drawer.openActions[key] = open;
  render();
}
function actionExtra(event) {
  const chunks = [];
  if (event.detail) chunks.push(event.detail);
  if (event.output) chunks.push(event.output);
  const summary = String(event.summary || "");
  if (!event.detail && !event.path && summary && (summary.includes("\n") || summary.length > 72) && summary !== actionLabel(event)) {
    chunks.push(summary);
  }
  return chunks;
}
function actionNode(job, event, key, times) {
  const verb = verbOf(event);
  const label = actionLabel(event) || verb;
  const full = event.path || event.detail || event.summary || "";
  const extra = actionExtra(event);
  const count = times > 1 ? el("span", { class: "act-count", text: "×" + times }) : null;
  const spacer = el("span", { class: "chev-space", "aria-hidden": "true" });
  if (event.job_file && !extra.length) {
    return el("div", { class: "act", title: full },
      spacer,
      el("span", { class: "act-verb", text: verb }),
      el("button", {
        type: "button", class: "ev-link",
        onclick: () => openJobFile(job, event.job_file),
      }, el("span", { text: event.job_file })),
      count);
  }
  if (!extra.length) {
    return el("div", { class: "act", title: full },
      spacer,
      el("span", { class: "act-verb", text: verb }),
      el("span", { class: "act-label", text: label }),
      count);
  }
  const open = actionOpen(key);
  const row = el("button", {
    type: "button", class: "act", "aria-expanded": String(open), title: full,
    onclick: () => toggleAction(key, !open),
  },
    el("span", { class: "chev-space", "aria-hidden": "true" }, el("span", { class: "chev small" })),
    el("span", { class: "act-verb", text: verb }),
    el("span", { class: "act-label", text: label }),
    count);
  const box = el("div", { class: "act-wrap" + (open ? " open" : "") }, row);
  if (open) extra.forEach((text) => box.append(el("pre", { class: "ev-out", text })));
  return box;
}
function exploreNode(job, events, key) {
  const open = actionOpen(key);
  const head = el("button", {
    type: "button", class: "explore", "aria-expanded": String(open),
    onclick: () => toggleAction(key, !open),
  },
    el("span", { class: "chev small", "aria-hidden": "true" }),
    el("span", { text: exploreLabel(events) }));
  const box = el("div", { class: "explore-wrap" + (open ? " open" : "") }, head);
  if (open) {
    const list = el("div", { class: "explore-list" });
    events.forEach((event, at) => list.append(actionNode(job, event, key + "#" + at, 1)));
    box.append(list);
  }
  return box;
}
function shellCard(event) {
  const command = shellCommand(event);
  const output = String(event.output || "");
  const progs = programsLabel(command);
  const head = el("div", { class: "sh-head" },
    el("span", { class: "sh-icon", "aria-hidden": "true", text: ">_" }),
    el("span", { class: "sh-title", text: "Ran command" }));
  if (progs) head.append(el("span", { class: "sh-progs", text: progs }));
  const card = el("div", { class: "sh" },
    head,
    el("div", { class: "sh-body" },
      el("pre", { class: "sh-cmd" }, el("span", { class: "sh-prompt", text: "$ " }), command)));
  if (output) card.querySelector(".sh-body").append(el("pre", { class: "sh-out", text: output }));
  return card;
}
function editCard(event) {
  const target = editTarget(event);
  const full = String(event.path || event.summary || target);
  return el("div", { class: "sh sh-edit" },
    el("div", { class: "sh-head" },
      el("span", { class: "sh-icon", "aria-hidden": "true", text: "±" }),
      el("span", { class: "sh-title", text: "Edited" }),
      el("span", { class: "sh-progs", text: tickName(target) })),
    el("div", { class: "sh-body" },
      el("pre", { class: "sh-path", text: full })));
}
function toolCard(event) {
  const name = toolName(event);
  const label = actionLabel(event);
  const detail = String(event.detail || "");
  const bodyText = detail && detail !== label ? detail : (label && label !== name ? label : "");
  const head = el("div", { class: "sh-head" },
    el("span", { class: "sh-icon", "aria-hidden": "true", text: "·" }),
    el("span", { class: "sh-title", text: "Used" }),
    el("span", { class: "sh-progs", text: tickName(name) }));
  const card = el("div", { class: "sh sh-tool" }, head);
  if (bodyText) {
    card.append(el("div", { class: "sh-body" },
      el("pre", { class: "sh-meta", text: bodyText })));
  }
  return card;
}
function feedNode(job, item, key) {
  if (item.type === "text") return el("p", { class: "feed-text" + (item.result ? " result" : ""), text: item.text });
  if (item.type === "heading") return el("p", { class: "feed-step", text: item.text });
  if (item.type === "explore") return exploreNode(job, item.events, key);
  if (item.type === "shell") return shellCard(item.event);
  if (item.type === "edit") return editCard(item.event);
  if (item.type === "tool") return toolCard(item.event);
  return actionNode(job, item.event, key, 1);
}
function timeline(job, run, error) {
  const box = el("div", { class: "d-doc d-timeline", tabindex: 0, "data-focus": "drawer-timeline" });
  if (error) box.append(el("p", { class: "error", text: error }));
  const heading = [run.agent || "Agent"];
  if (run.model) heading.push(run.model);
  box.append(el("div", { class: "run-head" },
    el("h4", { text: heading.join("  ·  ") }),
    el("p", { text: runMeta(run) })));
  const feed = feedOf(run);
  if (!feed.length) {
    box.append(el("p", { class: "kempty", text: run.status === "running" ? "Waiting for the first action." : "No actions recorded." }));
    return box;
  }
  const key = workKey(run);
  const open = workOpen(run);
  const running = run.status === "running";
  const worked = run.duration_s == null ? "" : " for " + durationLabel(run.duration_s);
  box.append(el("button", {
    type: "button", class: "worked", "aria-expanded": String(open),
    onclick: () => toggleWork(key, !open),
  },
    el("span", { text: (running ? "Working" : "Worked") + worked }),
    el("span", { class: "chev small", "aria-hidden": "true" }),
    running ? el("i", { class: "pulse", "aria-hidden": "true" }) : null));
  if (open) {
    const list = el("div", { class: "feed" });
    feed.forEach((item, index) => list.append(feedNode(job, item, runIdentity(run) + "#" + index)));
    box.append(list);
  }
  return box;
}
function activityBody(job) {
  if (!jobRecordsActivity(job)) {
    return el("div", { class: "d-activity" }, el("p", { class: "kempty", text: ACTIVITY_NOTICE }));
  }
  const data = state.drawer.activity;
  if (!data) return el("div", { class: "d-activity" }, el("p", { class: "kempty", text: "Loading..." }));
  const runs = Array.isArray(data.runs) ? data.runs : [];
  if (!runs.length) {
    return el("div", { class: "d-activity" },
      data.error ? el("p", { class: "error", text: data.error }) : null,
      el("p", { class: "kempty", text: data.notice || "No agent runs yet." }));
  }
  let index = state.drawer.run || 0;
  if (index >= runs.length) index = runs.length - 1;
  if (index < 0) index = 0;
  return el("div", { class: "d-body", style: splitStyle() },
    runList(job, runs, index), grip(), timeline(job, runs[index], data.error || ""));
}
function drawer() {
  if (!state.drawer) return null;
  if (state.drawer.kind === "issue") return issueDrawer();
  const job = jobs().find((item) => sameDrawer(item));
  const close = el("button", { type: "button", class: "close", "data-close": "true", "data-focus": "drawer-close", text: "close", onclick: closeDrawer });
  if (!job) {
    return el("div", { class: "overlay" }, el("div", { class: "scrim", onclick: closeDrawer }),
      el("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": "Job" },
        el("div", { class: "d-head" }, el("h3", { text: "That job is gone." }), close)));
  }
  const files = sortedFiles(job.files || []);
  return el("div", { class: "overlay" },
    el("div", { class: "scrim", onclick: closeDrawer }),
    el("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": "d-title" },
      el("div", { class: "d-head" },
        el("div", { class: "num", text: (state.workspace && state.selected === "all" && job.projectName ? job.projectName + " · " : "") + job.id + " · " + (job.step || "") + (job.round ? " · round " + job.round : "") }),
        el("h3", { id: "d-title", text: job.title || job.id }),
        el("div", { class: "links" },
          link(job.issue_url, "issue " + issueRef(job.issue)),
          prLinks(job, "PR #")),
        el("div", { class: "tabs d-tabs", role: "tablist", "aria-label": "Job view" },
          drawerTab("files", "Files"), drawerTab("activity", "Agent activity")),
        close),
      state.drawer.tab === "activity"
        ? activityBody(job)
        : el("div", { class: "d-body", style: splitStyle() }, fileList(job, files), grip(), fileView({ path: state.drawer.path, text: state.drawer.text || "", extra: "d-doc", focus: "drawer-doc" }))));
}
function day(stamp) {
  const found = /^\d{4}-\d{2}-\d{2}/.exec(String(stamp || ""));
  return found ? found[0] : "";
}
function labelChips(labels) {
  return (Array.isArray(labels) ? labels : []).map((name) => el("span", { class: "chip", text: name }));
}
function issueDrawerState(row) {
  return { kind: "issue", project: row.project, issue: String(row.issue), row, data: null, error: null, focus: true };
}
function sameIssue(row) {
  return !!(state.drawer && state.drawer.kind === "issue" && state.drawer.project === row.project && state.drawer.issue === String(row.issue));
}
function issueUrl(drawer) {
  return "/projects/" + encodeURIComponent(drawer.project) + "/issues/" + encodeURIComponent(drawer.issue);
}
function openIssue(row) {
  if (sameIssue(row)) return;
  state.drawer = issueDrawerState(row);
  render();
  fetchIssue(state.drawer);
}
async function fetchIssue(drawer) {
  const fallback = "Could not load issue " + issueRef(drawer.issue) + ".";
  let data = null;
  let error = null;
  try {
    const res = await fetch(issueUrl(drawer), { cache: "no-store" });
    const body = await res.json().catch(() => null);
    if (res.ok && body) data = body;
    else error = (body && body.error) || fallback;
  } catch (failure) {
    error = fallback;
  }
  if (state.drawer !== drawer) return;
  drawer.data = data;
  drawer.error = error;
  render();
}
function queueCard(row) {
  const who = state.workspace && state.selected === "all" && row.projectName ? row.projectName + " · " : "";
  const label = (row.title || row.note || "issue " + issueRef(row.issue)) + (state.workspace && state.selected === "all" && row.projectName ? ", " + row.projectName : "");
  const opened = day(row.created_at);
  return el("article", {
    class: "kcard" + (sameIssue(row) ? " on" : ""), tabindex: 0, role: "button", "aria-label": label,
    "data-focus": "queue:" + (row.project || "") + ":" + row.issue,
    onclick: (event) => { if (event.target.closest("a")) return; openIssue(row); },
    onkeydown: (event) => {
      if (event.target.closest("a")) return;
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openIssue(row); }
    },
  },
    el("div", { class: "card-top" },
      el("span", { class: "num", text: who + (row.action === "watch" ? row.note : row.action || "new") }),
      el("span", { class: "flag", text: issueRef(row.issue) })),
    el("p", { class: "feature", text: row.title || row.note || ("issue " + issueRef(row.issue)) }),
    el("div", { class: "links" }, link(row.issue_url, "issue " + issueRef(row.issue))),
    opened || row.author || (row.labels || []).length ? el("div", { class: "meta" },
      opened ? el("span", { text: opened }) : null,
      opened ? ageNode(row.created_at, "ago") : null,
      row.author ? el("span", { text: person(row.author, null, row.project, row.issue) }) : null,
      labelChips(row.labels)) : null,
    row.action === "hold" && row.title && row.note ? el("p", { class: "hint", text: row.note }) : null);
}
function plural(count, word) {
  return count + " " + word + (count === 1 ? "" : "s");
}
function issueBody(drawer) {
  const box = (...kids) => el("div", { class: "d-doc d-issue", tabindex: 0, "data-focus": "drawer-issue" }, kids);
  if (drawer.error) return box(el("p", { class: "error", text: drawer.error }));
  const data = drawer.data;
  if (!data) return box(el("p", { class: "kempty", text: "Loading..." }));
  const comments = Array.isArray(data.comments) ? data.comments : [];
  return box(
    data.body ? el("pre", { class: "file", text: data.body }) : el("p", { class: "kempty", text: "No description." }),
    el("h4", { class: "block-title", text: "Comments" }),
    comments.length
      ? comments.map((comment) => el("section", { class: "d-comment" },
        el("div", { class: "step-meta", text: [comment.author ? person(comment.author, null, drawer.project, drawer.issue) : "", day(comment.created_at)].filter(Boolean).join(" · ") }),
        el("pre", { class: "file", text: comment.body || "" })))
      : el("p", { class: "kempty", text: "No comments." }));
}
function issueDrawer() {
  const drawer = state.drawer;
  const row = state.queueRows.find((item) => item.project === drawer.project && String(item.issue) === drawer.issue) || drawer.row;
  const data = drawer.data || {};
  const project = state.projects.find((item) => item.id === drawer.project);
  const close = el("button", { type: "button", class: "close", "data-close": "true", "data-focus": "drawer-close", text: "close", onclick: closeDrawer });
  const issueLink = link(data.url || row.issue_url, "issue " + issueRef(drawer.issue));
  issueLink.setAttribute("data-focus", "drawer-issue-link");
  const author = data.author || row.author;
  const labels = drawer.data ? data.labels : row.labels;
  const created = data.created_at || row.created_at;
  const updated = data.updated_at || row.updated_at;
  const dated = (word, stamp) => (day(stamp) ? el("span", {}, word + " " + day(stamp) + " · ", ageNode(stamp, "ago")) : null);
  return el("div", { class: "overlay" },
    el("div", { class: "scrim", onclick: closeDrawer }),
    el("aside", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-labelledby": "d-title" },
      el("div", { class: "d-head" },
        el("div", { class: "num", text: (project ? projectLabel(project) : drawer.project) + " · issue " + issueRef(drawer.issue) + (row.action ? " · " + row.action : "") }),
        el("h3", { id: "d-title", text: data.title || row.title || row.note || "issue " + issueRef(drawer.issue) }),
        el("div", { class: "links" }, issueLink),
        el("div", { class: "facts" },
          author ? el("span", { text: person(author, null, drawer.project, drawer.issue) }) : null,
          labelChips(labels),
          dated("opened", created),
          dated("updated", updated),
          drawer.data ? el("span", { text: plural(Number(data.comment_count) || 0, "comment") }) : null),
        close),
      issueBody(drawer)));
}
function column(col, shown) {
  const queued = col.queue ? col.queue.filter(queueMatches) : null;
  const mine = queued || shown.filter((job) => col.steps.includes(job.step));
  const who = columnWho(col);
  return el("section", { class: "kcol" + (col.cls ? " " + col.cls : ""), "aria-label": col.label },
    el("div", { class: "kcol-head" },
      el("h3", { text: col.label }),
      who ? el("span", { class: "who", text: who }) : null,
      el("span", { class: "count", text: String(mine.length) })),
    mine.length ? mine.map((item) => col.queue ? queueCard(item) : jobCard(item)) : el("p", { class: "kempty", text: "—" }));
}
const ACTIVITY_NOTICE = "Agent activity is recorded for Claude Code and Cursor.";
function projectTools(project) {
  return project && Array.isArray(project.tools) ? project.tools : [];
}
function recordsActivity(tools) {
  return tools.indexOf("claude") >= 0 || tools.indexOf("cursor") >= 0;
}
function jobRecordsActivity(job) {
  if (!state.workspace) {
    const tools = state.index && Array.isArray(state.index.tools) ? state.index.tools : [];
    return recordsActivity(tools);
  }
  const project = state.projects.find((item) => item.id === job.project);
  return recordsActivity(projectTools(project));
}
function visibleRecordsActivity() {
  if (!state.workspace) return jobRecordsActivity({});
  return visibleProjects().some((project) => recordsActivity(projectTools(project)));
}
function nowKey(job) {
  return (job.project || "") + "\n" + job.id;
}
function lastTool(run) {
  let tool = null;
  (run.events || []).forEach((event) => {
    if (!event || event.kind === "text" || event.text) return;
    tool = event;
  });
  return tool;
}
function nowAction(tool) {
  const node = el("span", { class: "now-action" });
  if (!tool) {
    node.append(document.createTextNode("has not acted"));
    return node;
  }
  node.append(document.createTextNode(tool.name || ""));
  if (tool.summary) {
    node.append(document.createTextNode(" "));
    node.append(document.createTextNode(tool.summary));
  }
  return node;
}
function nowRow(job, run) {
  const who = state.workspace && state.selected === "all" && job.projectName
    ? job.projectName + " · " + job.id
    : job.id;
  const tool = lastTool(run);
  const started = tool && typeof run.started_at === "string" && run.started_at ? run.started_at : "";
  return el("div", { class: "now-row" },
    el("span", { class: "now-job", text: who }),
    el("span", { class: "now-agent", text: run.agent || "" }),
    el("span", { class: "now-step", text: run.step || "" }),
    started ? el("span", { class: "now-elapsed", "data-started": started }) : null,
    nowAction(tool));
}
function nowPanel(shown) {
  const head = el("h2", { text: "Now" });
  if (!visibleRecordsActivity()) {
    return el("section", { class: "now", "aria-label": "Now" },
      head, el("p", { class: "kempty", text: ACTIVITY_NOTICE }));
  }
  const rows = [];
  shown.forEach((job) => {
    if (!jobRecordsActivity(job)) return;
    const body = state.now && state.now[nowKey(job)];
    if (!body || !Array.isArray(body.runs)) return;
    body.runs.forEach((run) => {
      if (run && run.status === "running") rows.push(nowRow(job, run));
    });
  });
  if (nowPending(shown)) {
    return el("section", { class: "now", "aria-label": "Now" },
      head, el("p", { class: "kempty", text: "Loading..." }));
  }
  return el("section", { class: "now", "aria-label": "Now" },
    head, rows.length ? el("div", { class: "now-rows" }, rows) : el("p", { class: "kempty", text: "Nothing running" }));
}
function nowPending(shown) {
  return shown.some((job) => jobRecordsActivity(job) && !(state.nowSeen && state.nowSeen[nowKey(job)]));
}
function nowPanelKey() {
  if (!visibleRecordsActivity()) return "notice";
  const shown = jobs().filter(matches);
  if (nowPending(shown)) return "loading";
  const rows = [];
  shown.forEach((job) => {
    if (!jobRecordsActivity(job)) return;
    const body = state.now && state.now[nowKey(job)];
    if (!body || !Array.isArray(body.runs)) return;
    body.runs.forEach((run) => {
      if (!run || run.status !== "running") return;
      const events = (run.events || []).map((event) => {
        if (!event || event.kind === "text" || event.text) return null;
        return { name: event.name || "", summary: event.summary || "" };
      });
      rows.push({
        job: nowKey(job),
        agent: run.agent || "",
        step: run.step || "",
        started: typeof run.started_at === "string" ? run.started_at : "",
        events,
      });
    });
  });
  return JSON.stringify(rows);
}
function boardSub() {
  if (!state.workspace) return (state.index.project && state.index.project.description) || "Jobs by step";
  const projects = visibleProjects();
  if (projects.length === 1) return projects[0].description || projects[0].path || "Jobs by step";
  return projects.length + " projects";
}
function problemLines() {
  if (!state.workspace) return [];
  const lines = [];
  const notices = [];
  visibleProjects().forEach((project) => {
    const who = state.selected === "all" ? (project.name || project.id) + ": " : "";
    if (project.error) lines.push(who + String(project.error));
    if (project.notice) notices.push(who + String(project.notice));
  });
  state.queueErrors.forEach((item) => {
    const text = String(item);
    if (state.selected === "all" || text.indexOf(state.selected + ": ") === 0) lines.push(text);
  });
  return lines.map((text) => el("p", { class: "error", text }))
    .concat(notices.map((text) => el("p", { class: "stale", text })));
}
function board() {
  const all = jobs();
  const shown = all.filter(matches);
  const cols = boardColumns(all);
  const kanban = () => (cols.length
    ? el("div", { class: "kanban", "data-scroll-key": "factory" }, cols.map((col) => column(col, shown)))
    : el("p", { class: "kempty", text: state.workspace && !state.projects.length ? "no projects yet" : "no jobs" }));
  const head = (count) => el("div", { class: "head" },
    el("div", {}, el("h1", { text: "Board" }), el("p", { class: "sub", text: boardSub() })),
    count ? el("span", { class: "src", text: count }) : null);
  // Files served without the dashboard's server have no Jira or GitHub routes: the factory alone.
  if (!state.workspace) return withDrawer([head(shown.length + " of " + all.length), ...problemLines(), nowPanel(shown), kanban()]);
  const projects = visibleProjects();
  const nodes = [head(null)];
  if (projects.length) nodes.push(jiraBlock(projects), githubBlock(projects));
  nodes.push(blockSection("factory", "Factory", shown.length + " of " + all.length, [],
    () => [...problemLines(), nowPanel(shown), kanban()]));
  return withDrawer(nodes);
}
function withDrawer(nodes) {
  const panel = drawer();
  if (panel) nodes.push(panel);
  return nodes;
}

// ---- the board's blocks: Jira boards and GitHub issues ----
// Each block folds, and the browser remembers which. A block's nodes are reused while what they show is
// unchanged, so the 5 s refresh of jobs does not rebuild hundreds of cards.
function blockOpen(id) {
  return !state.collapsed.has(id);
}
function toggleBlock(id) {
  if (state.collapsed.has(id)) state.collapsed.delete(id);
  else state.collapsed.add(id);
  prefs.set(BLOCKS_KEY, Array.from(state.collapsed));
  render();
  if (blockOpen(id)) refreshFeeds(false);
}
function safeId(id) {
  return String(id).replace(/[^A-Za-z0-9_-]/g, "-");
}
function foldButton(id, title, extra) {
  const open = blockOpen(id);
  return el("button", {
    type: "button", class: "fold", "aria-expanded": open ? "true" : "false", "aria-controls": "body-" + safeId(id),
    "data-focus": "fold:" + id, onclick: () => toggleBlock(id),
  }, icon("m9 6 6 6-6 6"), el("span", { class: "fold-title", text: title }), extra || null);
}
function blockSection(id, title, count, tools, build) {
  const open = blockOpen(id);
  const counted = count === null || count === undefined ? null : el("span", { class: "count", text: String(count) });
  return el("section", { class: "block" + (open ? "" : " folded"), "aria-label": title },
    el("div", { class: "block-head" },
      el("h2", {}, foldButton(id, title, counted)),
      el("div", { class: "block-tools" }, tools)),
    el("div", { class: "block-body", id: "body-" + safeId(id), hidden: !open }, open ? build() : null));
}
function memo(id, key, build) {
  const hit = state.memo.get(id);
  if (hit && hit.key === key) return hit.node;
  const node = build();
  state.memo.set(id, { key, node });
  return node;
}
function drawerKey() {
  const d = state.drawer;
  return d ? [d.kind || "job", d.project, d.issue || d.id].join(":") : "";
}
function refreshButton(name, block) {
  return el("button", {
    type: "button", class: "refresh", title: "Read again", "aria-label": "Read " + name + " again",
    "data-focus": "refresh:" + block, onclick: () => refreshFeeds(true, block),
  }, icon("M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"));
}
function updatedNode(entries) {
  const times = entries.filter((entry) => entry && entry.at).map((entry) => entry.at);
  if (!times.length) return null;
  return el("span", { class: "src" }, "updated ", ageNode(new Date(Math.min(...times)).toISOString(), "ago"));
}
// Jira writes "+0000"; a browser reads "+00:00" for certain.
function isoStamp(stamp) {
  return typeof stamp === "string" ? stamp.replace(/([+-]\d{2})(\d{2})$/, "$1:$2") : stamp;
}
function jobFor(projectId, key) {
  return jobs().find((job) => job.project === projectId && String(job.issue) === String(key)) || null;
}
function queuedIssue(projectId, key) {
  return state.queueRows.some((row) => row.project === projectId && String(row.issue) === String(key)
    && (row.action === "new" || row.action === "hold"));
}
function queueLabelOf(project) {
  return (project.labels && project.labels.queue) || "factory";
}
// "job 016 · review": the job's number and where it is, short enough for a card.
function jobChip(job) {
  const number = String(job.id || "").split("-")[0];
  return el("span", { class: "chip " + kindOf(job), title: job.id, text: "job " + number + " · " + (job.waiting ? "Needs you" : job.step) });
}
function textMatches(values) {
  const q = state.query.trim().toLowerCase();
  return !q || values.some((value) => String(value || "").toLowerCase().includes(q));
}

// ---- reading the blocks' data ----
function feedUrl(project, tail, fresh) {
  return "/projects/" + encodeURIComponent(project) + tail + (fresh ? "?fresh=1" : "");
}
async function getFeed(url) {
  try {
    const res = await fetch(url, { cache: "no-store" });
    const body = await res.json().catch(() => null);
    if (res.ok && body) return { data: body, error: null };
    return { data: null, error: (body && body.error) || "Could not load it (" + res.status + ")." };
  } catch (error) {
    return { data: null, error: "Could not reach the dashboard's server." };
  }
}
// Keep an answer; true when it differs from the one before, so the page draws again only then.
function keepFeed(holder, key, found) {
  const json = JSON.stringify([found.data, found.error]);
  const before = holder[key];
  holder[key] = { at: Date.now(), data: found.data, error: found.error, json };
  return !before || before.json !== json;
}
function jiraHolder(id) {
  if (!state.feeds.jira[id]) state.feeds.jira[id] = { boards: null, board: {} };
  return state.feeds.jira[id];
}
function boardFold(projectId, board) {
  return "jira:" + projectId + ":" + board;
}
// The boards picked for a project in this browser, or the project's own board until one is picked.
function pickedBoards(id) {
  const saved = prefs.get("mfactory.jira.boards." + id, null);
  if (Array.isArray(saved)) return saved.filter((n) => Number.isInteger(n) && n > 0);
  const entry = state.feeds.jira[id] && state.feeds.jira[id].boards;
  const configured = entry && entry.data ? entry.data.configured : null;
  return Number.isInteger(configured) && configured > 0 ? [configured] : [];
}
// Feeds being read now, by address: a feed is never asked for twice at once.
const feedsInFlight = new Set();
let drawQueued = false;
// Draw once for every feed that landed since the last frame, so a fast feed never waits for a slow one.
function drawSoon() {
  if (drawQueued) return;
  drawQueued = true;
  const draw = () => {
    drawQueued = false;
    if (route().view === "board") render();
  };
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(draw);
  else setTimeout(draw, 0);
}
async function loadFeed(holder, key, url) {
  const id = url.split("?")[0];
  if (feedsInFlight.has(id)) return;
  feedsInFlight.add(id);
  try {
    if (keepFeed(holder, key, await getFeed(url))) drawSoon();
  } finally {
    feedsInFlight.delete(id);
  }
}
// Read the feeds that are open and old, or all of them with `force`. `only` limits it to one block.
function refreshFeeds(force, only) {
  if (!state.workspace || route().view !== "board") return;
  const now = Date.now();
  const due = (entry) => force || !entry || now - entry.at >= FEED_MS;
  visibleProjects().forEach((project) => {
    const id = project.id;
    if ((!only || only === "jira") && blockOpen("jira")) {
      const holder = jiraHolder(id);
      const boards = () => {
        const data = holder.boards && holder.boards.data;
        if (!data || data.off || data.note) return;
        pickedBoards(id).forEach((board) => {
          if (blockOpen(boardFold(id, board)) && due(holder.board[board])) {
            loadFeed(holder.board, board, feedUrl(id, "/jira/boards/" + board, force));
          }
        });
      };
      if (due(holder.boards)) loadFeed(holder, "boards", feedUrl(id, "/jira/boards", force)).then(boards);
      else boards();
    }
    if ((!only || only === "github") && blockOpen("github") && due(state.feeds.github[id])) {
      loadFeed(state.feeds.github, id, feedUrl(id, "/github/issues", force));
    }
  });
}

// ---- the Jira block ----
function jiraBlock(projects) {
  const many = projects.length > 1;
  const shown = projects.filter((project) => {
    const entry = state.feeds.jira[project.id] && state.feeds.jira[project.id].boards;
    return !(many && entry && entry.data && entry.data.off);
  });
  const entries = [];
  let boards = 0;
  shown.forEach((project) => {
    const holder = state.feeds.jira[project.id];
    if (!holder) return;
    entries.push(holder.boards);
    const data = holder.boards && holder.boards.data;
    if (data && !data.off && !data.note) {
      const picked = pickedBoards(project.id);
      boards += picked.length;
      picked.forEach((board) => entries.push(holder.board[board]));
    }
  });
  const tools = [updatedNode(entries), refreshButton("Jira", "jira")];
  return blockSection("jira", "Jira", boards || null, tools, () => (shown.length
    ? shown.map((project) => jiraArea(project, many))
    : el("p", { class: "kempty", text: "No project here takes jobs from Jira." })));
}
function jiraArea(project, many) {
  const id = project.id;
  const entry = state.feeds.jira[id] && state.feeds.jira[id].boards;
  const area = (...kids) => el("div", { class: "jira-area" }, many ? el("h3", { class: "block-title", text: projectLabel(project) }) : null, kids);
  if (!entry) return area(el("p", { class: "kempty", text: "Loading..." }));
  if (entry.error) return area(el("p", { class: "error", text: entry.error }));
  const data = entry.data;
  if (data.off || data.note) return area(el("p", { class: "notice", text: data.off || data.note }));
  const picked = pickedBoards(id);
  const names = picked.map((board) => {
    const found = (data.boards || []).find((item) => item.id === board);
    return found ? found.name : "board " + board;
  });
  return area(
    el("div", { class: "jira-tools" },
      el("button", {
        type: "button", class: "picker-open", "aria-haspopup": "dialog", "data-focus": "picker:" + id,
        onclick: () => openPicker(id),
      }, "Boards: ", el("span", { text: names.length ? names.join(", ") : "none chosen" }), " ▾")),
    picked.length ? picked.map((board) => jiraBoard(project, data, board))
      : el("p", { class: "kempty", text: "No board chosen. Choose one or several to see them here." }));
}
function jiraBoard(project, boards, board) {
  const fold = boardFold(project.id, board);
  const info = (boards.boards || []).find((item) => item.id === board) || null;
  const entry = jiraHolder(project.id).board[board];
  const view = entry && entry.data;
  const title = (view && view.board && view.board.name) || (info && info.name) || "Board " + board;
  const facts = [info && info.project, (view && view.board && view.board.type) || (info && info.type)].filter(Boolean).join(" · ");
  const url = (view && view.board && view.board.url) || (info && info.url);
  const open = blockOpen(fold);
  return el("section", { class: "jboard", "aria-label": title },
    el("div", { class: "jboard-head" },
      el("h3", {}, foldButton(fold, title, facts ? el("span", { class: "dim", text: facts }) : null)),
      url ? link(url, "open in Jira") : null,
      view && view.truncated ? el("span", { class: "src", text: "showing " + view.shown + " of " + view.total }) : null),
    el("div", { id: "body-" + safeId(fold), hidden: !open }, open ? boardBody(project, board, entry) : null));
}
function dropBoard(project, board) {
  prefs.set("mfactory.jira.boards." + project, pickedBoards(project).filter((n) => n !== board));
  render();
}
function boardBody(project, board, entry) {
  if (!entry) return el("p", { class: "kempty", text: "Loading..." });
  if (entry.error) {
    return el("div", { class: "board-error" },
      el("p", { class: "error", text: entry.error }),
      el("button", { type: "button", class: "drop-board", "data-focus": "drop:" + project.id + ":" + board,
        text: "Remove this board", onclick: () => dropBoard(project.id, board) }));
  }
  const scroll = "jira:" + project.id + ":" + board;
  return memo(scroll, [entry.json, state.query, state.jobsStamp, state.queueStamp, drawerKey()].join("\n"), () =>
    el("div", { class: "kanban jira", "data-scroll-key": scroll }, (entry.data.columns || []).map((col, index) => {
      const cards = (col.cards || []).filter((card) => textMatches([card.key, card.title, card.status, card.assignee, card.type, ...(card.labels || [])]));
      return el("section", { class: "kcol", "aria-label": col.name },
        el("div", { class: "kcol-head" }, el("h3", { text: col.name }), el("span", { class: "count", text: String(cards.length) })),
        el("div", { class: "kcol-cards", "data-scroll-key": scroll + ":" + index },
          cards.length ? cards.map((card) => jiraCard(project, card)) : el("p", { class: "kempty", text: "—" })));
    })));
}
function priorityClass(priority) {
  return /highest|high|blocker|critical/i.test(String(priority || "")) ? "hot" : "";
}
function jiraCard(project, card) {
  const job = jobFor(project.id, card.key);
  const row = { project: project.id, issue: card.key, title: card.title, issue_url: card.url, labels: card.labels,
    author: null, created_at: null, updated_at: isoStamp(card.updated_at), action: "" };
  const open = () => (job ? openDrawer(job) : openIssue(row));
  const on = job ? sameDrawer(job) : sameIssue(row);
  return el("article", {
    class: "kcard" + (job ? " job" : "") + (on ? " on" : ""), tabindex: 0, role: "button",
    "aria-label": card.key + " " + card.title, "data-focus": "jcard:" + project.id + ":" + card.key,
    onclick: (event) => { if (event.target.closest("a")) return; open(); },
    onkeydown: (event) => {
      if (event.target.closest("a")) return;
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    },
  },
    el("div", { class: "card-top" }, el("span", { class: "num", text: card.key }), job ? jobChip(job) : null),
    el("p", { class: "feature", text: card.title }),
    el("div", { class: "meta" },
      card.type ? el("span", { text: card.type }) : null,
      card.priority ? el("span", { class: priorityClass(card.priority), text: card.priority }) : null,
      card.assignee ? el("span", { text: card.assignee }) : null,
      card.updated_at ? ageNode(isoStamp(card.updated_at), "ago") : null,
      labelChips(card.labels)));
}

// ---- the board picker: a dialog outside #view, so a refresh of the page never rebuilds it ----
function openPicker(project) {
  state.jiraPicker = { project, query: "" };
  renderPicker();
}
function closePicker() {
  const picker = state.jiraPicker;
  if (!picker) return;
  state.jiraPicker = null;
  renderPicker();
  restoreFocus(document.getElementById("view"), "picker:" + picker.project);
  refreshFeeds(false);
}
function renderPicker() {
  const layer = document.getElementById("layer");
  if (!layer) return;
  const picker = state.jiraPicker;
  if (!picker) {
    layer.replaceChildren();
    return;
  }
  const project = state.projects.find((item) => item.id === picker.project);
  const entry = state.feeds.jira[picker.project] && state.feeds.jira[picker.project].boards;
  const boards = entry && entry.data && Array.isArray(entry.data.boards) ? entry.data.boards : [];
  const list = el("div", { class: "picker-list", role: "group", "aria-label": "Boards" });
  const foot = el("span", { class: "src" });
  const input = el("input", {
    type: "search", class: "picker-search", placeholder: "Filter boards...", "aria-label": "Filter boards",
    autocomplete: "off", spellcheck: "false",
    oninput: (event) => { picker.query = event.target.value; fillPicker(list, foot, boards); },
  });
  const dialog = el("div", { class: "picker", role: "dialog", "aria-modal": "true", "aria-labelledby": "picker-title", onkeydown: keepFocusIn },
    el("div", { class: "picker-head" },
      el("h3", { id: "picker-title", text: "Choose Jira boards" }),
      project ? el("span", { class: "src", text: projectLabel(project) }) : null),
    input, list,
    el("div", { class: "picker-foot" }, foot, el("button", { type: "button", class: "pick-done", text: "Done", onclick: closePicker })));
  layer.replaceChildren(el("div", { class: "overlay picker-layer" }, el("div", { class: "scrim", onpointerdown: closePicker }), dialog));
  fillPicker(list, foot, boards);
  input.focus();
}
function fillPicker(list, foot, boards) {
  const picker = state.jiraPicker;
  const chosen = new Set(pickedBoards(picker.project));
  const q = picker.query.trim().toLowerCase();
  const shown = boards.filter((board) => !q || [board.name, board.project, board.type, String(board.id)].some((value) => String(value || "").toLowerCase().includes(q)));
  const groups = new Map();
  shown.forEach((board) => {
    const key = board.project || "Other";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(board);
  });
  // Boards picked here that Jira no longer shows (deleted, or out of reach): listed ticked, so they can go.
  const known = new Set(boards.map((board) => board.id));
  const lost = Array.from(chosen).filter((id) => !known.has(id) && (!q || ("#" + id).includes(q)))
    .map((id) => ({ id, name: "Board " + id, project: "", type: "not found" }));
  if (lost.length) groups.set("No longer available", lost);
  const nodes = Array.from(groups.entries()).map(([key, items]) => el("div", { class: "picker-group" },
    el("h4", { text: key }),
    items.map((board) => el("label", { class: "picker-item" },
      el("input", { type: "checkbox", checked: chosen.has(board.id), onchange: (event) => pickBoard(picker.project, board.id, event.target.checked, foot) }),
      el("span", { class: "picker-name", text: board.name }),
      el("span", { class: "dim", text: [board.type, "#" + board.id].filter(Boolean).join(" · ") })))));
  list.replaceChildren(...(nodes.length ? nodes : [el("p", { class: "kempty", text: boards.length ? "No board matches." : "No boards to choose from." })]));
  foot.textContent = chosen.size + " chosen";
}
function pickBoard(project, board, on, foot) {
  const picked = pickedBoards(project).filter((n) => n !== board);
  if (on) picked.push(board);
  prefs.set("mfactory.jira.boards." + project, picked);
  foot.textContent = picked.length + " chosen";
  render();
  refreshFeeds(false);
}
function keepFocusIn(event) {
  if (event.key !== "Tab") return;
  const items = Array.from(event.currentTarget.querySelectorAll("input, button"));
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

// ---- the GitHub issues block ----
const GH_FILTERS = [["all", "All"], ["mine", "Assigned to me"], ["outside", "Not in the factory"]];
function setGhFilter(name) {
  state.ghFilter = name;
  prefs.set(GH_FILTER_KEY, name);
  render();
}
function filteredIssues(project, data) {
  return (data.issues || []).filter((issue) => {
    if (!textMatches(["#" + issue.number, issue.title, issue.author, ...(issue.assignees || []), ...(issue.labels || [])])) return false;
    if (state.ghFilter === "mine") return (issue.assignees || []).indexOf(data.me) >= 0;
    if (state.ghFilter === "outside") {
      return !jobFor(project.id, issue.number) && !queuedIssue(project.id, issue.number)
        && (issue.labels || []).indexOf(queueLabelOf(project)) < 0;
    }
    return true;
  });
}
function githubBlock(projects) {
  const many = projects.length > 1;
  const parts = projects.map((project) => {
    const entry = state.feeds.github[project.id];
    return { project, entry, list: entry && entry.data ? filteredIssues(project, entry.data) : [] };
  });
  const count = parts.reduce((sum, part) => sum + part.list.length, 0);
  const tools = [
    el("div", { class: "tabs", role: "group", "aria-label": "Which issues" }, GH_FILTERS.map(([name, label]) => el("button", {
      type: "button", "aria-pressed": state.ghFilter === name ? "true" : "false", "data-focus": "ghf:" + name,
      text: label, onclick: () => setGhFilter(name),
    }))),
    updatedNode(parts.map((part) => part.entry)), refreshButton("GitHub issues", "github")];
  return blockSection("github", "GitHub issues", count, tools, () => parts.map((part) => githubArea(part, many)));
}
function githubArea(part, many) {
  const { project, entry, list } = part;
  const area = (...kids) => el("div", { class: "gh-area" }, many ? el("h3", { class: "block-title", text: projectLabel(project) }) : null, kids);
  if (!entry) return area(el("p", { class: "kempty", text: "Loading..." }));
  if (entry.error) return area(el("p", { class: "error", text: entry.error }));
  const data = entry.data;
  const rows = memo("gh:" + project.id, [entry.json, state.query, state.ghFilter, state.jobsStamp, state.queueStamp, drawerKey()].join("\n"), () => (list.length
    ? el("div", { class: "irows" }, list.map((issue) => issueRow(project, issue)))
    : el("p", { class: "kempty", text: state.query || state.ghFilter !== "all" ? "No issue matches." : "No open issues." })));
  const more = data.truncated
    ? el("p", { class: "src more" }, "The " + (data.issues || []).length + " most recently updated. ", link(data.repo_url ? data.repo_url + "/issues" : null, "All issues on GitHub"))
    : null;
  return area(rows, more);
}
function issueRow(project, issue) {
  const job = jobFor(project.id, issue.number);
  const label = queueLabelOf(project);
  const queued = !job && (queuedIssue(project.id, issue.number) || (issue.labels || []).indexOf(label) >= 0);
  const row = { project: project.id, issue: String(issue.number), title: issue.title, issue_url: issue.url,
    labels: issue.labels, author: issue.author, created_at: issue.created_at, updated_at: issue.updated_at, action: "" };
  const open = () => openIssue(row);
  return el("article", {
    class: "irow" + (sameIssue(row) ? " on" : ""), tabindex: 0, role: "button", "aria-label": "#" + issue.number + " " + issue.title,
    "data-focus": "irow:" + project.id + ":" + issue.number,
    onclick: (event) => { if (event.target.closest("a")) return; open(); },
    onkeydown: (event) => {
      if (event.target.closest("a")) return;
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    },
  },
    el("span", { class: "num", text: "#" + issue.number }),
    el("span", { class: "ititle", text: issue.title }),
    el("span", { class: "ichips" },
      job ? jobChip(job) : queued ? el("span", { class: "chip run", text: "queued" }) : null,
      labelChips((issue.labels || []).filter((name) => !(queued && name === label)))),
    el("span", { class: "imeta" },
      (issue.assignees || []).length ? el("span", { text: issue.assignees.map((login) => "@" + login).join(", ") }) : null,
      issue.author ? el("span", { class: "dim", text: "by @" + issue.author }) : null,
      issue.updated_at ? ageNode(issue.updated_at, "ago") : null));
}

// ---- overview ----
function stepSeconds(job) {
  const out = {};
  for (const h of job.history || []) {
    const a = time(h.entered_at), b = time(h.at);
    if (a && b) out[h.step] = (out[h.step] || 0) + (b - a) / 1000;
  }
  return out;
}
function statCard(label, value, delta, path, accent, deltaClass) {
  return el("div", { class: "card" + (accent ? " accent" : "") },
    el("div", { class: "row" }, el("span", { class: "icon" }, icon(path)), delta ? el("span", { class: "delta " + (deltaClass || ""), text: delta }) : null),
    el("div", { class: "label", text: label }),
    el("div", { class: "value", text: value }));
}
function days(count) {
  const out = [], today = new Date();
  today.setHours(0, 0, 0, 0);
  for (let i = count - 1; i >= 0; i--) out.push(new Date(today.getTime() - i * 86400000));
  return out;
}
function slotNote() {
  const caps = state.workspace
    ? visibleProjects().map((project) => Number(project.concurrency)).filter((n) => n > 0)
    : [Number(state.index && state.index.concurrency)].filter((n) => n > 0);
  if (!caps.length) return "no cap";
  const cap = caps.reduce((sum, n) => sum + n, 0);
  const projects = state.workspace ? visibleProjects().length : 1;
  return projects > 1 ? "of " + cap + " slots · " + projects + " projects" : "of " + cap + " slots";
}
function areaChart(all) {
  const done = all.filter((j) => j.step === "done" && j.finished_at);
  let count = Number(state.range);
  if (state.range === "all") {
    const first = Math.min(...done.map((j) => time(j.finished_at)), Date.now());
    count = Math.min(365, Math.max(7, Math.ceil((Date.now() - first) / 86400000) + 1));
  }
  const axis = days(count);
  const daily = axis.map((day) => done.filter((j) => { const t = time(j.finished_at); return t >= day.getTime() && t < day.getTime() + 86400000; }).length);
  let sum = 0;
  const values = daily.map((v) => (sum += v));
  const total = sum;
  const W = 640, H = 230, L = 44, R = 8, T = 14, B = 32;
  const ticks = total <= 4 ? Math.max(2, total) : 4, top = Math.max(2, Math.ceil(total / ticks) * ticks);
  const x = (i) => L + (axis.length === 1 ? 0 : (i * (W - L - R)) / (axis.length - 1));
  const y = (v) => T + (1 - v / top) * (H - T - B);
  const root = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Jobs merged over " + count + " days, as a running total: " + total });
  const grad = svg("linearGradient", { id: "merged-fill", x1: 0, y1: 0, x2: 0, y2: 1 },
    svg("stop", { offset: "0", "stop-color": "#ff4a26", "stop-opacity": ".55" }), svg("stop", { offset: "1", "stop-color": "#ff4a26", "stop-opacity": ".04" }));
  root.append(svg("defs", {}, grad));
  for (let i = 0; i <= ticks; i++) {
    const v = Math.round((top * i) / ticks);
    root.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "#232323" }), svg("text", { x: L - 8, y: y(v) + 3, "text-anchor": "end", text: v }));
  }
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  root.append(svg("path", { d: `${line} L${x(values.length - 1)},${y(0)} L${x(0)},${y(0)} Z`, fill: "url(#merged-fill)" }), svg("path", { d: line, fill: "none", stroke: "#ff4a26", "stroke-width": 1.6 }));
  if (total) root.append(svg("line", { x1: x(0), x2: x(values.length - 1), y1: y(0), y2: y(total), stroke: "#d8d8d8", "stroke-dasharray": "4 4", "stroke-width": 1 }));
  const every = Math.max(1, Math.ceil(axis.length / 8));
  axis.forEach((day, i) => {
    const last = axis.length - 1;
    if (i !== last && (i % every || last - i < every * 0.7)) return;
    root.append(svg("text", { x: x(i), y: H - 8, "text-anchor": i === 0 ? "start" : i === axis.length - 1 ? "end" : "middle", text: day.toLocaleDateString(undefined, { month: "short", day: "numeric" }) }));
  });
  return { root, total, mean: total / values.length };
}
function claudeOn() {
  if (state.workspace) return visibleProjects().some((project) => (project.tools || []).includes("claude"));
  return !!(state.index && (state.index.tools || []).includes("claude"));
}
function filled(map) {
  return !!map && typeof map === "object" && Object.keys(map).length > 0;
}
function percent(v) {
  if (!v) return "0%";
  return v < 0.1 ? "<0.1%" : (v < 10 ? v.toFixed(1) : Math.round(v)) + "%";
}
function resetText(at) {
  if (at === null || at === undefined || at === "") return "";
  const d = typeof at === "number" ? new Date(at < 1e12 ? at * 1000 : at) : new Date(at);
  if (isNaN(d.getTime())) return "";
  const clock = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (d.getTime() - Date.now() < 86400000) return "resets " + clock;
  return "resets " + d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) + " " + clock;
}
function planCards() {
  const snap = state.planUsage;
  if (!snap || !claudeOn()) return null;
  const card = (key, label, path) => {
    const w = snap[key];
    if (!w || typeof w.used_percentage !== "number") return null;
    const used = w.used_percentage;
    return statCard(label, Math.round(used) + "%", resetText(w.resets_at), path, false, used >= 80 ? "hot" : "flat");
  };
  const cards = [
    card("five_hour", "Session (5hr)", "M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z"),
    card("seven_day", "Weekly (7 day)", "M4 6h16M4 6v14h16V6M8 3v6M16 3v6"),
  ].filter(Boolean);
  if (!cards.length) return null;
  return el("div", { class: "cards plan", title: "Claude Code plan usage, as /usage shows it. Seen " + new Date(snap.at).toLocaleString() }, cards);
}
function planByStep(all) {
  const measured = all.some((j) => filled(j.plan_by_step));
  const shared = all.some((j) => filled(j.usage_share_by_step));
  if (!claudeOn() || (!measured && !shared)) return null;
  const totals = {};
  for (const job of all) {
    for (const [step, used] of Object.entries(job.plan_by_step || {})) {
      const t = (totals[step] = totals[step] || { week: 0, session: 0, share: 0 });
      t.week += Number(used.seven_day) || 0;
      t.session += Number(used.five_hour) || 0;
    }
    for (const [step, share] of Object.entries(job.usage_share_by_step || {})) {
      const t = (totals[step] = totals[step] || { week: 0, session: 0, share: 0 });
      t.share += Number(share) || 0;
    }
  }
  const main = (t) => (measured ? t.week : t.share);
  const rows = Object.entries(totals).filter(([, t]) => main(t) > 0 || t.share > 0).sort((a, b) => main(b[1]) - main(a[1]) || b[1].share - a[1].share).slice(0, 6);
  return { measured, rows };
}
function byStep(all) {
  const priced = all.some((j) => j.cost_usd !== null && j.cost_usd !== undefined);
  const totals = {};
  for (const job of all) {
    const source = priced ? job.cost_by_step || {} : stepSeconds(job);
    for (const [step, v] of Object.entries(source)) {
      const info = stepOf(step, job);
      if (!priced && info && info.type === "human") continue;
      totals[step] = (totals[step] || 0) + v;
    }
  }
  const rows = Object.entries(totals).sort((a, b) => b[1] - a[1]).slice(0, 6);
  const sum = Object.values(totals).reduce((a, b) => a + b, 0);
  return { priced, rows, sum };
}
function jobTable(list) {
  if (!list.length) return el("div", { class: "empty", text: state.query ? "No job matches the search." : "No jobs yet. " + String(trackerInfo().enqueue || "Label an issue").replace(/`/g, "") + " and run /mfactory run." });
  const body = el("tbody", {}, list.map((job) => el("tr", {
    class: "link", tabindex: 0,
    onclick: () => { location.hash = jobHref(job); },
    onkeydown: (e) => { if (e.key === "Enter") location.hash = jobHref(job); },
  },
    el("td", { class: "mono", text: state.workspace && state.selected === "all" && job.projectName ? job.projectName + " · " + job.id : job.id }),
    el("td", { class: "wrap", text: job.title || "" }),
    el("td", {}, chip(job)),
    el("td", { class: "mono dim", text: job.round || "-" }),
    el("td", { class: "mono dim", text: jobPrs(job).map((p) => "#" + p.number).join(" ") || "-" }),
    el("td", { class: "mono dim", text: job.cost_usd !== null && job.cost_usd !== undefined ? money(job.cost_usd) : "-" }),
    el("td", { class: "mono dim" }, elapsedNode(job)))));
  return el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ["Job", "Title", "Step", "Round", "PR", "Cost", "Time"].map((t) => el("th", { scope: "col", text: t })))), body));
}
function overviewSub() {
  if (!state.workspace) return (state.index.project && state.index.project.description) || "Running slots, merged jobs, and every job";
  const projects = visibleProjects();
  if (!projects.length) return "No projects yet";
  if (projects.length === 1) return projects[0].description || projects[0].path || "Running slots, merged jobs, and every job";
  return projects.length + " projects";
}
function overview() {
  const all = jobs();
  const running = all.filter((j) => { const s = stepOf(j.step, j); return s && RUNNING.has(s.type) && !j.waiting; });
  const merged = all.filter((j) => j.step === "done");
  const week = merged.filter((j) => time(j.finished_at) > Date.now() - 7 * 86400000).length;
  const waiting = all.filter((j) => j.waiting);
  const authors = waiting.filter((j) => j.waiting.who === "author").length;
  const named = waiting.filter((j) => Array.isArray(j.waiting.who)).length;
  const rest = waiting.length - authors - named;
  const waitingParts = [authors + " author", rest + " maintainer"];
  if (named) waitingParts.push(named + " named");
  const waitingNote = waitingParts.filter((part) => !part.startsWith("0 ")).join(" · ");
  const split = byStep(all);
  const total = all.reduce((sum, j) => sum + (Number(j.cost_usd) || 0), 0);
  const finished = all.filter((j) => j.finished_at && j.step === "done").map((j) => (time(j.finished_at) - time(j.created_at)) / 1000).sort((a, b) => a - b);
  const median = finished.length ? finished[Math.floor(finished.length / 2)] : null;
  const chart = areaChart(all);
  const rangeTab = (key, text) => el("button", { type: "button", "aria-pressed": String(state.range === key), text, onclick: () => { state.range = key; render(); } });
  const shown = all.filter(matches);
  return [
    el("div", { class: "head" },
      el("div", {}, el("h1", { text: "Overview" }), el("p", { class: "sub", text: overviewSub() })),
      el("span", { class: "src", text: shown.length + " of " + all.length })),
    ...problemLines(),
    planCards(),
    el("div", { class: "cards" },
      statCard("Running", String(running.length), slotNote(), "M5 3l14 9-14 9z", true),
      statCard("Merged", String(merged.length), week ? "+" + week + " this week" : "none this week", "M5 12l5 5L20 7", false, week ? "" : "flat"),
      statCard("Needs you", String(waiting.length), waiting.length ? waitingNote : "nothing waiting", "M12 8v5M12 17h.01M12 3a9 9 0 100 18 9 9 0 000-18z", false, waiting.length ? "hot" : "flat"),
      split.priced ? statCard("Cost", money(total), "Claude Code", "M12 3v18M16 7H10a3 3 0 000 6h4a3 3 0 010 6H7", false, "flat")
        : statCard("Median time to merge", span(median), merged.length + " merged", "M12 7v5l3 2M12 3a9 9 0 100 18 9 9 0 000-18z", false, "flat")),
    el("div", { class: "split" },
      el("div", { class: "panel" },
        el("div", { class: "head" }, el("div", {}, el("h2", { text: "Jobs merged" }), el("p", { class: "sub", text: "Running total for the period. Dashed line: a steady pace" })),
          el("div", { class: "tabs", role: "group", "aria-label": "Period" }, rangeTab("7", "7D"), rangeTab("30", "30D"), rangeTab("all", "ALL"))),
        chart.root,
        el("div", { class: "figures" },
          el("div", {}, "Merged in period", el("b", { text: String(chart.total) })),
          el("div", {}, "Per day", el("b", { text: chart.mean.toFixed(1) })),
          el("div", {}, "Merged of finished", el("b", { class: "good", text: all.some((j) => j.finished_at) ? Math.round((100 * merged.length) / all.filter((j) => j.finished_at).length) + "%" : "-" })))),
      stepPanel(all, split)),
    el("div", { class: "panel" }, el("div", { class: "head" }, el("h2", { text: "Jobs" }), el("span", { class: "src", text: shown.length + " of " + all.length })), jobTable(shown)),
  ];
}
function stepPanel(all, split) {
  const plan = planByStep(all);
  const unit = plan && (state.unit === "plan" || !state.unit || !split.priced) ? "plan" : "usd";
  const unitTab = (key, text) => el("button", { type: "button", "aria-pressed": String(unit === key), text, onclick: () => { state.unit = key; render(); } });
  const tabs = plan && split.priced ? el("div", { class: "tabs", role: "group", "aria-label": "Unit" }, unitTab("plan", "PLAN"), unitTab("usd", "API $")) : null;
  let title = split.priced ? "Cost by step" : "Time by step";
  let sub = split.priced ? "API-equivalent $, all jobs" : "Agents and scripts, all jobs";
  let bars;
  if (unit === "plan") {
    title = "Cost by step";
    sub = plan.measured
      ? "Weekly plan limit used, measured before and after each step. Approximate"
      : "Share of this machine's Claude Code usage over 7 days. Approximate";
    const main = (t) => (plan.measured ? t.week : t.share);
    const top = plan.rows.length ? main(plan.rows[0][1]) || 1 : 1;
    bars = plan.rows.map(([step, t]) => {
      const extra = [];
      if (plan.measured) extra.push("5hr " + percent(t.session));
      if (plan.measured && t.share) extra.push(percent(t.share) + " of usage");
      return el("div", { class: "bar" },
        el("div", { class: "row" }, el("span", { class: "name", text: step }), el("span", { class: "num", text: percent(main(t)) + (plan.measured ? " weekly" : " of usage") })),
        el("div", { class: "track" }, el("div", { class: "fill", style: "width:" + Math.max(2, (100 * main(t)) / top).toFixed(1) + "%" })),
        el("div", { class: "pct", text: extra.join(" · ") }));
    });
  } else {
    bars = split.rows.map(([step, v]) => el("div", { class: "bar" },
      el("div", { class: "row" }, el("span", { class: "name", text: step }), el("span", { class: "num", text: split.priced ? money(v) : span(v) })),
      el("div", { class: "track" }, el("div", { class: "fill", style: "width:" + Math.max(2, (100 * v) / split.rows[0][1]).toFixed(1) + "%" })),
      el("div", { class: "pct", text: ((100 * v) / split.sum).toFixed(1) + "%" })));
  }
  return el("div", { class: "panel" },
    el("div", { class: "head" }, el("div", {}, el("h2", { text: title }), el("p", { class: "sub", text: sub })), tabs),
    bars.length ? el("div", { class: "bars" }, bars) : el("div", { class: "empty", text: "Nothing has run yet." }));
}
// ---- pipeline ----
function order(graph) {
  // The first edge of each step is its normal way on; that chain is the spine.
  // A step off the spine sits right under the last spine step that leads to it,
  // except the escalation step: any step can lead there, so it goes last.
  const steps = new Map(graph.steps.map((s) => [s.name, s]));
  const spine = [];
  let name = graph.start;
  while (name && steps.has(name) && !spine.includes(name)) {
    spine.push(name);
    const step = steps.get(name);
    const next = step.enabled === false ? step.edges.find((e) => e.outcome === step.skip) : step.edges.find((e) => !spine.includes(e.to));
    name = next ? next.to : null;
  }
  const out = spine.slice();
  let moved = true;
  while (moved) {
    moved = false;
    for (const step of graph.steps) {
      if (out.includes(step.name) || step.name === graph.escalate_to) continue;
      const from = out.filter((n) => steps.get(n).edges.some((e) => e.to === step.name));
      if (!from.length) continue;
      let at = Math.max(...from.map((n) => out.indexOf(n))) + 1;
      while (at < out.length && !spine.includes(out[at])) at++;
      out.splice(at, 0, step.name);
      moved = true;
    }
  }
  for (const step of graph.steps) if (!out.includes(step.name)) out.push(step.name);
  const ends = graph.end_states.slice().sort((x, y) => (x === "done" ? -1 : y === "done" ? 1 : x < y ? -1 : 1));
  return out.concat(ends);
}
// ---- pipelines: one or more per project, each a YAML file ----
function pipelinesOf(project) {
  const found = project && project.pipelines && Array.isArray(project.pipelines.pipelines) ? project.pipelines.pipelines : [];
  if (found.length) return found;
  return project && project.graph ? [{ name: "default", description: "", file: "", graph: project.graph, yaml: "" }] : [];
}
function staticPipelines() {
  if (state.pipelines && Array.isArray(state.pipelines.pipelines) && state.pipelines.pipelines.length) return state.pipelines.pipelines;
  return state.graph ? [{ name: "default", description: "", file: "", graph: state.graph, yaml: "" }] : [];
}
function pipelineGraphFor(project, name) {
  const list = project ? pipelinesOf(project) : staticPipelines();
  const found = list.find((p) => p.name === (name || "default")) || list.find((p) => p.name === "default") || list[0];
  return found && found.graph ? found.graph : (project ? project.graph : state.graph) || null;
}
function pipeTab(holder, name, label, sub) {
  const on = state[holder] === name;
  return el("button", {
    type: "button", role: "tab", class: "ptab", "aria-selected": String(on), "aria-pressed": on ? "true" : "false",
    "data-focus": holder + ":" + name, onclick: () => { state[holder] = name; prefs.set("mfactory." + holder, name); render(); },
  }, el("span", { class: "mono", text: label }), sub ? el("span", { class: "dim", text: sub }) : null);
}
function pickedPipeline(holder, list) {
  if (!list.some((p) => p.name === state[holder])) return list[0] ? list[0].name : null;
  return state[holder];
}
function stepTable(graph, count) {
  const off = (step) => step.enabled === false;
  const rows = graph.steps.map((step) => el("tr", {},
    el("td", { class: "mono", text: step.name }),
    el("td", {}, el("span", { class: "chip" + (off(step) ? " off" : ""), text: step.type + (off(step) ? " · off" : "") }),
      step.who ? el("div", { class: "dim step-who", text: whoText(step.who) }) : null),
    el("td", { class: "mono wrap" }, (step.edges || []).map((e) => el("div", { class: off(step) && e.outcome !== step.skip ? "dim" : "" },
      e.outcome, el("span", { class: "dim", text: " → " }), e.to, e.max !== null && e.max !== undefined ? el("span", { class: "dim", text: `  max ${e.max}` }) : null))),
    el("td", { class: "mono dim", text: count(step.name) || "-" })));
  return el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, ["Step", "Type", "Outcomes", "Jobs"].map((t) => el("th", { scope: "col", text: t })))), el("tbody", {}, rows)));
}
function pipelineBlock(project, jobList) {
  const list = project ? pipelinesOf(project) : staticPipelines();
  const holder = "pipeView";
  if (!list.length) return [el("div", { class: "notice", text: "No pipe for this project yet." })];
  const name = pickedPipeline(holder, list);
  const chosen = list.find((p) => p.name === name) || list[0];
  const graph = chosen.graph;
  const mine = jobList.filter((j) => (j.pipeline || "default") === chosen.name);
  const count = (step) => mine.filter((j) => j.step === step).length;
  const nodes = [];
  if (list.length > 1) {
    nodes.push(el("div", { class: "ptabs", role: "tablist", "aria-label": "Pipes" },
      list.map((p) => pipeTab(holder, p.name, p.name, String(jobList.filter((j) => (j.pipeline || "default") === p.name).length) + " jobs"))));
  }
  if (!graph || !Array.isArray(graph.steps)) return nodes.concat([el("div", { class: "notice", text: "This pipe could not be drawn. See Config for its problems." })]);
  nodes.push(el("p", { class: "sub" },
    chosen.description ? chosen.description + " · " : "",
    `Starts at ${graph.start}. When an edge's max is used up, the job goes to ${graph.escalate_to}.`,
    chosen.file ? el("span", { class: "dim", text: "  " + chosen.file }) : null));
  nodes.push(el("div", { class: "panel pipe-graph" }, dagView(graph, { counts: count, zoomKey: "pipe:" + (project ? project.id : "") + ":" + chosen.name, label: "Pipe " + chosen.name }), dagLegend(false)));
  nodes.push(el("div", { class: "panel" }, el("h2", { text: "Steps" }), el("p", { class: "sub", text: chosen.file && chosen.file !== "built-in" ? "As declared in " + chosen.file : "The built-in pipe" }), stepTable(graph, count)));
  return nodes;
}
function pipeline() {
  const nodes = [el("h1", { text: "Pipes" })];
  if (!state.workspace) return nodes.concat(pipelineBlock(null, jobs()));
  const projects = visibleProjects();
  if (!projects.length) return nodes.concat([el("p", { class: "kempty", text: "no projects yet" })]);
  projects.forEach((project) => {
    if (projects.length > 1) nodes.push(el("h2", { class: "block-title", text: project.name || project.id }));
    if (project.error) nodes.push(el("p", { class: "error", text: String(project.error) }));
    nodes.push(...pipelineBlock(project, jobs().filter((job) => job.project === project.id)));
  });
  return nodes;
}

// ---- jobs ----
function sameFile(job) {
  return !!(state.file && state.file.job === job.id && (state.file.project || null) === (job.project || null));
}
async function showFile(job, path) {
  if (!sameFile(job) || state.file.path !== path) state.raw = false;
  state.file = { job: job.id, project: job.project || null, path, text: "Loading..." };
  render();
  let text;
  try {
    const res = await fetch(fileUrl(job, path), { cache: "no-store" });
    text = res.ok ? await res.text() : `Could not load ${path} (${res.status}).`;
  } catch (e) {
    text = `Could not load ${path}.`;
  }
  if (sameFile(job) && state.file.path === path) { state.file.text = text; render(); }
}
function jobHref(job) {
  if (state.workspace) return "#/jobs/" + encodeURIComponent(job.project) + "/" + encodeURIComponent(job.id);
  return "#/jobs/" + encodeURIComponent(job.id);
}
function sameJob(job, projectId, id) {
  if (!job || !id || job.id !== id) return false;
  if (!state.workspace || !projectId) return true;
  return job.project === projectId;
}
function queueLabel() {
  if (state.index && state.index.labels && state.index.labels.queue) return state.index.labels.queue;
  for (const project of visibleProjects()) {
    if (project.labels && project.labels.queue) return project.labels.queue;
  }
  return "factory";
}
// A project's tracker, as index.json describes it. Empty for a project whose core is older than trackers.
function trackerInfo(projectId) {
  const own = projectId && state.workspace ? state.projects.find((item) => item.id === projectId) : null;
  if (own && own.tracker && own.tracker.kind) return own.tracker;
  if (state.index && state.index.tracker) return state.index.tracker;
  for (const project of visibleProjects()) {
    if (project.tracker && project.tracker.kind) return project.tracker;
  }
  return {};
}
// Which tracker holds an issue: the job says so, or the key's shape does (a number is GitHub's).
function trackerOfKey(key, tracker) {
  if (tracker === "github" || tracker === "jira") return tracker;
  return /^#?\d+$/.test(String(key == null ? "" : key)) ? "github" : "jira";
}
// A person as the page shows them: a GitHub login gets "@"; Jira knows people by name, or by an account id.
// With an issue's key the issue's own tracker decides, so a project with both trackers names each right.
function person(login, name, projectId, key, tracker) {
  if (name) return String(name);
  const text = String(login || "");
  const kind = key !== undefined && key !== null ? trackerOfKey(key, tracker) : trackerInfo(projectId).kind;
  return kind === "jira" ? text : "@" + text;
}
// A human step's `who` as text: a role, logins, or a table of logins per tracker.
function whoText(who) {
  if (Array.isArray(who)) return who.join(", ");
  if (who && typeof who === "object") {
    return Object.keys(who).map((kind) => kind + ": " + (Array.isArray(who[kind]) ? who[kind].join(", ") : String(who[kind]))).join(" · ");
  }
  return who ? String(who) : "";
}
// How issues join the queue, from each tracker's own words: "Label an issue `factory`, or move a card to ...".
function enqueueHint() {
  for (const project of visibleProjects()) {
    const hints = (project.trackers || []).map((item) => item && item.enqueue).filter(Boolean);
    if (hints.length) return hints[0] + hints.slice(1).map((hint) => ", or " + hint.charAt(0).toLowerCase() + hint.slice(1)).join("");
  }
  return trackerInfo().enqueue;
}
// "Label an issue `factory`" as text and code nodes, from the tracker's own words.
function enqueueNodes() {
  const hint = enqueueHint();
  if (!hint) return ["Label an issue ", el("code", { text: queueLabel() })];
  return String(hint).split("`").map((part, index) => (index % 2 ? el("code", { text: part }) : part));
}
// ---- jobs: a table of every job, and a page per job with its pipeline drawn ----
const STATUS_ORDER = ["running", "waiting", "queued", "done", "failed", "rejected"];
const STATUS_LABEL = { running: "Running", waiting: "Waiting", queued: "Queued", done: "Done", failed: "Failed", rejected: "Rejected" };
const JOB_TABS = [["graph", "Graph"], ["gantt", "Timeline"], ["details", "Details"], ["files", "Files"], ["logs", "Logs"]];
const BACK = /^(FAIL|FAILED|REJECT|CHANGES|BLOCKED|ESCALATE|UNKNOWN|NEEDS-INFO|FEEDBACK)$/;
function statusOf(job) {
  if (job.status && STATUS_LABEL[job.status]) return job.status;
  if (job.step === "done" || job.step === "failed" || job.step === "rejected") return job.step;
  if (job.waiting) return "waiting";
  return job.running_since ? "running" : "queued";
}
function statusPill(job) {
  const st = statusOf(job);
  return el("span", { class: "pill st-" + st }, st === "running" ? el("i", { class: "pulse", "aria-hidden": "true" }) : el("i", { class: "dot", "aria-hidden": "true" }), STATUS_LABEL[st]);
}
function jobSeconds(job) {
  const start = time(job.created_at);
  if (!start) return null;
  return ((time(job.finished_at) || Date.now()) - start) / 1000;
}
// Time a job spent being worked on, and waiting for people, from its steps' visits.
function jobTimes(job) {
  const graph = graphOf(job);
  const types = new Map(((graph && graph.steps) || []).map((s) => [s.name, s.type]));
  let work = 0, wait = 0;
  (job.history || []).forEach((h) => {
    const s = (time(h.at) - time(h.entered_at)) / 1000;
    if (!(s >= 0)) return;
    if (types.get(h.step) === "human") wait += s; else work += s;
  });
  if (job.step && !isEnd(job.step, job) && time(job.entered_at)) {
    const s = (Date.now() - time(job.entered_at)) / 1000;
    if (types.get(job.step) === "human") wait += s; else work += s;
  }
  return { work, wait };
}
function runCells(job) {
  const graph = graphOf(job);
  const states = job.step_states || {};
  if (!graph || !Array.isArray(graph.steps)) return el("span", { class: "dim", text: "-" });
  const shown = graph.steps.filter((s) => s.enabled !== false || (states[s.name] && states[s.name].state !== "disabled"));
  return el("span", { class: "cells", "aria-hidden": "true" }, shown.map((s) => {
    const st = (states[s.name] && states[s.name].state) || "none";
    return el("i", { class: "cell st-" + st, title: s.name + ": " + DAG_STATES[st].label });
  }));
}
function sortValue(job, key) {
  switch (key) {
    case "status": return STATUS_ORDER.indexOf(statusOf(job));
    case "job": return job.id;
    case "title": return String(job.title || "").toLowerCase();
    case "pipeline": return job.pipeline || "default";
    case "step": return job.step || "";
    case "round": return job.round || 0;
    case "started": return time(job.created_at) || 0;
    case "duration": return jobSeconds(job) || 0;
    case "updated": return time(job.updated_at) || 0;
    case "cost": return Number(job.cost_usd) || 0;
    default: return job.id;
  }
}
function sortJobs(list) {
  const sort = state.jobSort || { key: "started", dir: -1 };
  return list.slice().sort((a, b) => {
    const x = sortValue(a, sort.key), y = sortValue(b, sort.key);
    if (x < y) return -sort.dir;
    if (x > y) return sort.dir;
    return a.id < b.id ? 1 : -1;
  });
}
function sortHeader(key, label, cls) {
  const sort = state.jobSort || { key: "started", dir: -1 };
  const on = sort.key === key;
  return el("th", { scope: "col", class: cls || null, "aria-sort": on ? (sort.dir > 0 ? "ascending" : "descending") : null },
    el("button", {
      type: "button", class: "sort" + (on ? " on" : ""), "data-focus": "sort:" + key,
      onclick: () => {
        state.jobSort = { key, dir: on ? -sort.dir : (key === "title" || key === "job" || key === "pipeline" || key === "step" ? 1 : -1) };
        prefs.set("mfactory.jobs.sort", state.jobSort);
        render();
      },
    }, label, el("span", { class: "arrow", "aria-hidden": "true", text: on ? (sort.dir > 0 ? "▲" : "▼") : "↕" })));
}
function statusChips(all) {
  const counts = {};
  all.forEach((job) => { const st = statusOf(job); counts[st] = (counts[st] || 0) + 1; });
  const pick = state.jobFilter || "all";
  const chipFor = (id, label, n) => el("button", {
    type: "button", class: "fchip" + (id !== "all" ? " st-" + id : ""), "aria-pressed": String(pick === id), "data-focus": "jobfilter:" + id,
    onclick: () => { state.jobFilter = id; prefs.set("mfactory.jobs.filter", id); render(); },
  }, id !== "all" ? el("i", { class: "dot", "aria-hidden": "true" }) : null, label, el("b", { text: String(n) }));
  return el("div", { class: "fchips", role: "group", "aria-label": "Status" },
    chipFor("all", "All", all.length), STATUS_ORDER.filter((st) => counts[st]).map((st) => chipFor(st, STATUS_LABEL[st], counts[st])));
}
function pipelineSelect(all) {
  const names = [...new Set(all.map((job) => job.pipeline || "default"))].sort();
  if (names.length < 2) return null;
  return el("label", { class: "pselect" }, el("span", { class: "dim", text: "Pipe" }),
    el("select", { "data-focus": "jobpipe", onchange: (ev) => { state.jobPipe = ev.target.value; render(); } },
      el("option", { value: "all", text: "all", selected: state.jobPipe === "all" || !state.jobPipe }),
      names.map((n) => el("option", { value: n, text: n, selected: state.jobPipe === n }))));
}
function jobsTable(all) {
  const pick = state.jobFilter || "all";
  const shown = sortJobs(all.filter(matches)
    .filter((job) => pick === "all" || statusOf(job) === pick)
    .filter((job) => !state.jobPipe || state.jobPipe === "all" || (job.pipeline || "default") === state.jobPipe));
  const many = state.workspace && state.selected === "all" && visibleProjects().length > 1;
  const open = (job) => { location.hash = jobHref(job); };
  const rows = shown.map((job) => el("tr", {
    class: "link", tabindex: 0, "data-focus": "jobrow:" + (job.project || "") + ":" + job.id,
    onclick: (ev) => { if (!ev.target.closest("a")) open(job); },
    onkeydown: (ev) => { if (ev.key === "Enter") open(job); },
  },
    el("td", {}, statusPill(job)),
    el("td", { class: "mono opt-s" }, many && job.projectName ? el("div", { class: "dim", text: job.projectName }) : null, el("a", { href: jobHref(job), text: job.id })),
    el("td", { class: "wrap title" }, el("div", { text: job.title || "" }), el("div", { class: "dim mono", text: issueRef(job.issue) + (job.author ? " · " + person(job.author, job.author_name, job.project, job.issue, job.tracker) : "") })),
    el("td", { class: "opt" }, el("span", { class: "chip pipechip", text: job.pipeline || "default" })),
    el("td", {}, el("div", { class: "mono", text: job.step || "-" }), runCells(job)),
    el("td", { class: "mono dim opt", text: job.round || "-" }),
    el("td", { class: "mono dim opt" }, ageNode(job.created_at, "ago")),
    el("td", { class: "mono dim opt-s" }, elapsedNode(job)),
    el("td", { class: "mono dim opt" }, ageNode(job.updated_at, "ago")),
    el("td", { class: "mono dim opt", text: job.cost_usd !== null && job.cost_usd !== undefined ? money(job.cost_usd) : "-" }),
    el("td", { class: "mono dim opt" }, jobPrs(job).length ? prLinks(job, "#") : "-")));
  return [
    el("div", { class: "jobs-tools" }, statusChips(all), pipelineSelect(all)),
    el("div", { class: "panel flush" }, shown.length
      ? el("div", { class: "scroll" }, el("table", { class: "jobs" },
        el("thead", {}, el("tr", {},
          sortHeader("status", "Status"), sortHeader("job", "Job", "opt-s"), sortHeader("title", "Title"), sortHeader("pipeline", "Pipe", "opt"),
          sortHeader("step", "Step · run"), sortHeader("round", "Round", "opt"), sortHeader("started", "Started", "opt"),
          sortHeader("duration", "Duration", "opt-s"), sortHeader("updated", "Updated", "opt"), sortHeader("cost", "Cost", "opt"),
          el("th", { scope: "col", class: "opt", text: "PR" }))),
        el("tbody", {}, rows)))
      : el("div", { class: "empty", text: "No job matches." })),
  ];
}

// The files a step wrote, by the step's type and name.
function stepFiles(job, name, step) {
  const files = job.files || [];
  const type = step ? step.type : "";
  const test = (re) => files.filter((f) => re.test(f.path));
  if (type === "checks") return test(/^round-\d+\/checks(-\d+)?\.(md|log)$/);
  if (type === "review") return test(/^round-\d+\/review-/);
  if (type === "merge") return test(/^round-\d+\/merge-checks/);
  if (type === "verify") return test(/^verify/);
  if (type === "human") return test(/^feedback-\d+\.md$/);
  const own = { intake: /^intake(-\d+)?\.md$/, spec: /^spec(-\d+)?\.md$/, build: /^(build|progress)(-\d+)?\.md$/,
    debug: /^round-\d+\/debug/, approve: /^decision(-\d+)?\.md$/, "post-debug": /^round-\d+\/post-debug/ }[name];
  if (own) return test(own);
  const plain = new RegExp("^" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(-\\d+)?\\.md$");
  return test(plain);
}
function ensureJobActivity(job) {
  if (sameDrawer(job) && state.drawer.inline) return;
  state.drawer = {
    project: job.project || null, id: job.id, path: null, text: "", focus: false, inline: true,
    tab: "activity", run: 0, runPicked: false, activity: null, activitySeq: 0,
  };
  fetchActivity(job);
}
function pickNode(name) {
  state.jobNode = state.jobNode === name ? null : name;
  render();
}
function visitRows(info) {
  const visits = (info && info.visits) || [];
  if (!visits.length) return el("p", { class: "dim", text: "No finished visits." });
  return el("div", { class: "scroll" }, el("table", { class: "mini" },
    el("thead", {}, el("tr", {}, ["#", "Entered", "Took", "Outcome"].map((t) => el("th", { scope: "col", text: t })))),
    el("tbody", {}, visits.map((v, i) => el("tr", { class: BACK.test(v.outcome || "") ? "ko" : "ok" },
      el("td", { class: "mono dim", text: String(i + 1) }),
      el("td", { class: "mono dim", text: shortStamp(v.entered_at) }),
      el("td", { class: "mono", text: span(v.seconds) }),
      el("td", { class: "mono" }, v.outcome + " → " + v.to, v.reason ? el("div", { class: "dim history-reason", text: String(v.reason).split("\n")[0].slice(0, 160) }) : null))))));
}
function shortStamp(stamp) {
  const t = time(stamp);
  if (!t) return "-";
  const d = new Date(t);
  const today = new Date();
  const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  if (d.toDateString() === today.toDateString()) return hm;
  return d.toISOString().slice(5, 10) + " " + hm;
}
function nodePanel(job, name) {
  const graph = graphOf(job);
  const step = stepOf(name, job);
  const info = (job.step_states || {})[name] || { state: "none", visits: [] };
  const st = info.state || "none";
  const files = stepFiles(job, name, step);
  const runs = ((state.drawer && state.drawer.activity && state.drawer.activity.runs) || []).filter((r) => r.step === name);
  const cost = job.cost_by_step && job.cost_by_step[name];
  const live = name === job.step && !isEnd(name, job);
  return el("aside", { class: "panel nodepanel", "aria-label": "Step " + name },
    el("div", { class: "head" },
      el("div", {}, el("h2", { class: "mono", text: name }),
        el("div", { class: "dim", text: step ? step.type + (step.who ? " · " + whoText(step.who) : "") + (step.enabled === false ? " · off" : "") : isEnd(name, job) ? "end state" : "" })),
      el("span", { class: "pill st-" + st }, el("i", { class: "dot", "aria-hidden": "true" }), DAG_STATES[st].label),
      el("button", { type: "button", class: "close", text: "close", "data-focus": "node-close", onclick: () => pickNode(name) })),
    el("dl", { class: "kv" },
      el("dt", { text: "Runs" }), el("dd", { class: "mono", text: String((info.visits || []).length) + (live ? " + now" : "") }),
      el("dt", { text: "Time" }), el("dd", { class: "mono" }, span(info.seconds || 0), live ? el("span", { class: "dim" }, " + ", ageNode(job.entered_at, "span"), " now") : null),
      cost ? el("dt", { text: "Cost" }) : null, cost ? el("dd", { class: "mono", text: money(cost) }) : null,
      el("dt", { text: "Outcomes" }), el("dd", { class: "mono wrap" }, step ? (step.edges || []).map((e) => el("div", {}, e.outcome, el("span", { class: "dim", text: " → " + e.to + (e.max != null ? "  max " + e.max : "") }))) : "-")),
    el("h3", { text: "Visits" }), visitRows(info),
    el("h3", { text: "Files" }),
    files.length ? el("div", { class: "nfiles" }, files.map((f) => el("button", {
      type: "button", class: "nfile", "data-focus": "nfile:" + f.path,
      onclick: () => { state.jobTab = "files"; showFile(job, f.path); },
    }, el("i", { class: "dot " + (tone(f.first_line) || "") }), el("span", { class: "mono", text: f.path }), el("span", { class: "dim", text: f.first_line || "" })))) : el("p", { class: "dim", text: "No files from this step." }),
    el("h3", { text: "Agent runs" }),
    runs.length ? el("div", { class: "nruns" }, runs.map((r) => el("button", {
      type: "button", class: "nrun", onclick: () => { state.jobTab = "logs"; state.drawer.run = state.drawer.activity.runs.indexOf(r); state.drawer.runPicked = true; render(); },
    }, el("span", { class: "who", text: r.agent || "agent" }), el("span", { class: "dim", text: runMeta(r) })))) : el("p", { class: "dim", text: state.drawer && state.drawer.activity ? "No agent runs recorded for this step." : "Loading agent runs..." }),
  );
}
function jobGraphTab(job) {
  const graph = graphOf(job);
  if (!graph || !Array.isArray(graph.steps)) return el("div", { class: "notice", text: "This job's pipe is not available." });
  const selected = state.jobNode && (stepOf(state.jobNode, job) || isEnd(state.jobNode, job)) ? state.jobNode : null;
  const drawn = dagView(graph, {
    states: job.step_states || {}, taken: job.edges_taken || {}, selected, onSelect: pickNode,
    zoomKey: "job:" + (job.project || "") + ":" + job.id, label: "Pipe " + (job.pipeline || "default") + " for job " + job.id,
  });
  return el("div", { class: "jobgraph" + (selected ? " with-panel" : "") },
    el("div", { class: "panel pipe-graph" }, drawn, dagLegend(true),
      el("p", { class: "dim hint", text: "Click a step for its visits, files and agent runs." })),
    selected ? nodePanel(job, selected) : null);
}
// Each visit of a step as a bar on one time axis, like a scheduler's Gantt view.
function jobGanttTab(job) {
  const graph = graphOf(job);
  const types = new Map(((graph && graph.steps) || []).map((s) => [s.name, s.type]));
  const bars = (job.history || []).map((h) => ({ step: h.step, from: time(h.entered_at), to: time(h.at), outcome: h.outcome, next: h.to, live: false }))
    .filter((b) => b.from && b.to);
  if (job.step && !isEnd(job.step, job) && time(job.entered_at)) bars.push({ step: job.step, from: time(job.entered_at), to: Date.now(), outcome: null, next: null, live: true });
  if (!bars.length) return el("div", { class: "notice", text: "No step has finished yet." });
  const start = Math.min(...bars.map((b) => b.from));
  const end = Math.max(...bars.map((b) => b.to), start + 1000);
  const W = 1200, label = 150, row = 26, top = 28;
  const scale = (t) => label + ((t - start) / (end - start)) * (W - label - 16);
  const height = top + bars.length * row + 10;
  const chart = svg("svg", { class: "gantt", viewBox: `0 0 ${W} ${height}`, role: "img", "aria-label": "Timeline of " + job.id });
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const t = start + ((end - start) * i) / ticks;
    const x = scale(t);
    chart.append(svg("line", { class: "g-grid", x1: x, x2: x, y1: top - 6, y2: height - 6 }));
    chart.append(svg("text", { class: "g-tick", x, y: 14, "text-anchor": i === 0 ? "start" : i === ticks ? "end" : "middle", text: shortStamp(new Date(t).toISOString()) }));
  }
  bars.forEach((b, i) => {
    const y = top + i * row;
    const human = types.get(b.step) === "human";
    const st = b.live ? (human ? "waiting" : job.running_since ? "running" : "queued")
      : BACK.test(b.outcome || "") ? (b.next === "failed" || b.next === "rejected" ? "failed" : "retry") : "success";
    const x = scale(b.from), w = Math.max(3, scale(b.to) - x);
    const g = svg("g", { class: "g-row" + (state.jobNode === b.step ? " on" : ""), "data-step": b.step });
    g.append(svg("text", { class: "g-name", x: 8, y: y + 16, text: b.step }));
    g.append(svg("rect", { class: "g-bar st-" + st + (human ? " human" : ""), x, y: y + 4, width: w, height: row - 9, rx: 3 }));
    g.append(svg("title", { text: `${b.step}: ${span((b.to - b.from) / 1000)}` + (b.outcome ? ` · ${b.outcome} → ${b.next}` : b.live ? " · now" : "") }));
    if (w > 60) g.append(svg("text", { class: "g-dur", x: x + 6, y: y + 16, text: span((b.to - b.from) / 1000) }));
    g.addEventListener("click", () => { state.jobNode = b.step; state.jobTab = "graph"; render(); });
    chart.append(g);
  });
  const times = jobTimes(job);
  return el("div", { class: "panel" },
    el("div", { class: "head" }, el("h2", { text: "Timeline" }), el("span", { class: "dim mono", text: "working " + span(times.work) + " · waiting for people " + span(times.wait) })),
    el("div", { class: "scroll gantt-wrap" }, chart),
    el("div", { class: "legend dag-legend" }, ["success", "retry", "failed", "running", "waiting"].map((st) => el("span", {}, el("i", { class: "sw st-" + st }), DAG_STATES[st].label)),
      el("span", {}, el("i", { class: "sw human" }), "a person's turn")));
}
function barList(rows, format) {
  const top = Math.max(...rows.map((r) => r[1]), 0);
  if (!rows.length || !top) return el("p", { class: "dim", text: "Nothing recorded yet." });
  return el("div", { class: "bars" }, rows.map(([name, value]) => el("div", { class: "bar-row" },
    el("span", { class: "mono", text: name }),
    el("span", { class: "bar" }, el("i", { style: "width:" + Math.max(2, (value / top) * 100).toFixed(1) + "%" })),
    el("span", { class: "mono dim", text: format(value) }))));
}
function jobDetailsTab(job) {
  const times = jobTimes(job);
  const states = job.step_states || {};
  const byTime = Object.keys(states).map((n) => [n, states[n].seconds || 0]).filter((r) => r[1] > 0).sort((a, b) => b[1] - a[1]);
  const byCost = Object.entries(job.cost_by_step || {}).filter((r) => Number(r[1]) > 0).sort((a, b) => b[1] - a[1]);
  const kv = (label, value) => [el("dt", { text: label }), el("dd", {}, value === null || value === undefined || value === "" ? el("span", { class: "dim", text: "-" }) : value)];
  const mono = (text) => el("span", { class: "mono", text: String(text) });
  return el("div", { class: "details" },
    el("div", { class: "panel" }, el("h2", { text: "Job" }), el("dl", { class: "kv" },
      kv("Id", mono(job.id)), kv("Pipe", el("span", { class: "chip pipechip", text: job.pipeline || "default" })),
      kv("Status", statusPill(job)), kv("Step", mono(job.step || "-")),
      kv("Issue", link(job.issue_url, issueRef(job.issue))), kv("Tracker", job.tracker ? mono(job.tracker) : null),
      kv("Author", job.author ? el("span", { text: person(job.author, job.author_name, job.project, job.issue, job.tracker) }) : null),
      kv("Risk", job.intake && job.intake.risk ? el("span", { class: riskClass(job.intake.risk), text: job.intake.risk }) : null),
      kv("Surfaces", job.intake ? mono((job.intake.surfaces || []).join(", ") || "none") : null),
      kv("Round", mono(String(job.round || 0) + (job.attempt > 1 ? ", attempt " + job.attempt : ""))),
      kv("Escalated from", job.escalated_from ? mono(job.escalated_from) : null),
      kv("Reason", job.reason ? el("span", { class: "wrap", text: String(job.reason).split("\n")[0].slice(0, 300) }) : null))),
    el("div", { class: "panel" }, el("h2", { text: "Code" }), el("dl", { class: "kv" },
      kv("Branch", job.branch ? mono(job.branch) : null), kv("Worktree", job.worktree ? mono(job.worktree) : null),
      kv("Pull requests", jobPrs(job).length ? el("span", {}, prLinks(job, "#")) : null),
      kv("Merge commit", job.merge_sha ? mono(String(job.merge_sha).slice(0, 12)) : null),
      kv("Follow-ups", job.follow_ups ? mono(job.follow_ups) : null))),
    el("div", { class: "panel" }, el("h2", { text: "Time" }), el("dl", { class: "kv" },
      kv("Created", el("span", {}, mono(shortStamp(job.created_at)), el("span", { class: "dim" }, "  ", ageNode(job.created_at, "ago")))),
      kv("Finished", job.finished_at ? mono(shortStamp(job.finished_at)) : null),
      kv("Updated", ageNode(job.updated_at, "ago")),
      kv("Total", elapsedNode(job)), kv("Working", mono(span(times.work))), kv("Waiting for people", mono(span(times.wait)))),
      el("h3", { text: "Time by step" }), barList(byTime, span)),
    el("div", { class: "panel" }, el("h2", { text: "Cost" }), el("dl", { class: "kv" },
      kv("Total", job.cost_usd !== null && job.cost_usd !== undefined ? mono(money(job.cost_usd)) : null),
      kv("Unpriced models", (job.cost_unpriced_models || []).length ? mono(job.cost_unpriced_models.join(", ")) : null)),
      el("h3", { text: "Cost by step" }), barList(byCost, money)));
}
function fileSize(n) {
  if (!(n >= 0)) return "-";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
function jobFilesTab(job) {
  const files = sortedFiles(job.files || []);
  const open = sameFile(job) ? state.file : null;
  if (!files.length) return el("div", { class: "panel" }, el("div", { class: "empty", text: "No files yet." }));
  const graph = graphOf(job);
  const owner = (path) => {
    for (const s of (graph && graph.steps) || []) if (stepFiles({ files: [{ path }] }, s.name, s).length) return s.name;
    return "";
  };
  return el("div", { class: "panel" },
    el("div", { class: "scroll" }, el("table", { class: "files-table" },
      el("thead", {}, el("tr", {}, ["File", "Step", "First line", "Size", "Modified"].map((t, i) => el("th", { scope: "col", class: i >= 2 && i !== 3 ? "opt" : null, text: t })))),
      el("tbody", {}, files.map((f) => el("tr", {
        class: "link" + (open && open.path === f.path ? " on" : ""), tabindex: 0, "data-focus": "file:" + f.path,
        onclick: () => showFile(job, f.path), onkeydown: (ev) => { if (ev.key === "Enter") showFile(job, f.path); },
      },
        el("td", { class: "mono" }, el("i", { class: "dot " + (tone(f.first_line) || "") }), " ", fileLabel(f.path)),
        el("td", { class: "mono dim", text: owner(f.path) || "-" }),
        el("td", { class: "mono dim wrap opt", text: f.first_line || "" }),
        el("td", { class: "mono dim", text: fileSize(f.size) }),
        el("td", { class: "mono dim opt" }, f.modified_at ? ageNode(f.modified_at, "ago") : "-")))))),
    open ? el("div", { class: "file-open" }, el("div", { class: "head" }, el("h3", { class: "mono", text: open.path }),
      el("button", { type: "button", class: "close", text: "close", onclick: () => { state.file = null; render(); } })),
      fileView({ path: open.path, text: open.text })) : el("p", { class: "dim hint", text: "Pick a file to read it." }));
}
function jobLogsTab(job) {
  return el("div", { class: "panel logs" }, activityBody(job));
}
function jobTabButton(id, label) {
  const on = (state.jobTab || "graph") === id;
  return el("button", {
    type: "button", role: "tab", "aria-selected": String(on), "aria-pressed": on ? "true" : "false", "data-focus": "jobtab:" + id,
    onclick: () => { state.jobTab = id; prefs.set("mfactory.job.tab", id); render(); },
  }, label);
}
function kpi(label, value, cls) {
  return el("div", { class: "kpi" + (cls ? " " + cls : "") }, el("span", { class: "k", text: label }), el("span", { class: "v" }, value));
}
function waitingBox(job) {
  if (job.waiting) {
    return el("div", { class: "ask" },
      el("h2", {}, "Waiting for " + waitingAudience(job) + " since ", ageNode(job.waiting && job.waiting.since, "ago")),
      el("p", { text: job.reason || (job.waiting.replies === "tool"
        ? `Step ${job.step} needs a reply in your coding tool. A comment on the ${job.waiting.asked_on === "pr" ? "pull request" : "issue"} does not count.`
        : `Step ${job.step} needs a reply. Type one of these in your coding tool, or comment it on the ${job.waiting.asked_on === "pr" ? "pull request" : "issue"} without the number.`) }),
      commands(job).map((cmd) => el("div", { class: "cmd" }, el("code", { text: cmd }),
        el("button", { type: "button", text: "Copy", onclick: async (e) => {
          const literal = cmd.replace(/ <[^>]+>$/, " ");
          try { await navigator.clipboard.writeText(literal); e.target.textContent = "Copied"; } catch (_) { e.target.textContent = "Select it"; }
          setTimeout(() => { e.target.textContent = "Copy"; }, 1500);
        } }))));
  }
  if (job.reason && isEnd(job.step, job) && job.step !== "done") {
    return el("div", { class: "notice", style: "white-space:pre-wrap;overflow-wrap:anywhere", text: job.reason });
  }
  return null;
}
function jobPage(job) {
  ensureJobActivity(job);
  const times = jobTimes(job);
  const tab = state.jobTab || "graph";
  const body = tab === "gantt" ? jobGanttTab(job) : tab === "details" ? jobDetailsTab(job) : tab === "files" ? jobFilesTab(job) : tab === "logs" ? jobLogsTab(job) : jobGraphTab(job);
  const many = state.workspace && state.selected === "all" && job.projectName;
  return [
    el("nav", { class: "crumbs", "aria-label": "Breadcrumb" }, el("a", { href: "#/jobs", text: "Jobs" }), el("span", { class: "dim", text: " / " }),
      many ? el("span", { class: "dim", text: job.projectName + " / " }) : null, el("span", { class: "mono", text: job.id })),
    el("div", { class: "jobhead" },
      el("div", { class: "jh-main" },
        el("h1", { class: "job-title", text: job.title || job.id }),
        el("div", { class: "jh-meta" }, statusPill(job), el("span", { class: "chip pipechip", title: "pipe", text: job.pipeline || "default" }),
          el("span", { class: "mono dim", text: job.id }),
          link(job.issue_url, "issue " + issueRef(job.issue)), prLinks(job, "PR #"),
          job.branch ? el("span", { class: "mono dim", title: "branch", text: "⎇ " + job.branch }) : null))),
    el("div", { class: "kpis" },
      kpi("Step", el("span", { class: "mono", text: job.step || "-" })),
      kpi("Duration", elapsedNode(job)),
      kpi("Working", el("span", { class: "mono", text: span(times.work) })),
      kpi("Waiting for people", el("span", { class: "mono", text: span(times.wait) })),
      kpi("Round", el("span", { class: "mono", text: String(job.round || 0) + (job.attempt > 1 ? " · a" + job.attempt : "") })),
      kpi("Cost", el("span", { class: "mono", text: job.cost_usd !== null && job.cost_usd !== undefined ? money(job.cost_usd) : "-" })),
      kpi("Files", el("span", { class: "mono", text: String((job.files || []).length) }))),
    waitingBox(job),
    el("div", { class: "tabs jobtabs", role: "tablist", "aria-label": "Job view" }, JOB_TABS.map(([id, label]) => jobTabButton(id, label))),
    body,
  ].filter(Boolean);
}
function jobsView(id, projectId) {
  const all = jobs();
  if (!all.length) return [el("h1", { text: "Jobs" }), el("p", { class: "sub", text: trackerInfo().kind === "jira" ? "One job per card from the queue column" : "One job per labeled issue" }), el("div", { class: "notice" }, "No jobs yet. ", ...enqueueNodes(), " and run ", el("code", { text: "/mfactory run" }), " in your coding tool.")];
  if (id) {
    const job = all.find((item) => sameJob(item, projectId, id));
    if (job) return jobPage(job);
    return [el("nav", { class: "crumbs" }, el("a", { href: "#/jobs", text: "Jobs" })), el("div", { class: "notice", text: "No job " + id + " here. It may have been deleted." })];
  }
  const running = all.filter((job) => statusOf(job) === "running").length;
  const waiting = all.filter((job) => statusOf(job) === "waiting").length;
  return [
    el("div", { class: "head" },
      el("div", {}, el("h1", { text: "Jobs" }), el("p", { class: "sub", text: `${all.length} jobs · ${running} running · ${waiting} waiting for a person` + (state.query ? " · filtered by the search" : "") }))),
    ...jobsTable(all),
  ];
}

// ---- config ----
function configRows(rows) {
  const q = state.query.trim().toLowerCase();
  // A pipeline's steps are shown with the pipeline, from its YAML file.
  return rows.filter((r) => !/^steps\./.test(String(r.key)) && !/^run\.(start|escalate_to)$/.test(String(r.key))).filter((r) => (state.source === "all" || r.source === state.source) && (!q || String(r.key).toLowerCase().includes(q) || JSON.stringify(r.value).toLowerCase().includes(q)));
}
function configTable(rows) {
  if (!rows) return el("div", { class: "empty", text: "No config has been written for this project yet." });
  const shown = configRows(rows);
  if (!shown.length) return el("div", { class: "empty", text: "No setting matches." });
  return el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ["Key", "Value", "Source"].map((t) => el("th", { scope: "col", text: t })))),
    el("tbody", {}, shown.map((r) => el("tr", {}, el("td", { class: "mono", text: r.key }), el("td", { class: "mono wrap", text: JSON.stringify(r.value) }),
      el("td", {}, el("span", { class: "src" + (r.source === "factory.toml" ? " mine" : ""), text: r.source })))))));
}
function sourceFilters(rows) {
  const sources = ["all", ...new Set(configRows(rows || []).map((r) => r.source).filter((source) => source))];
  return el("div", { class: "filters", role: "group", "aria-label": "Source" }, sources.map((s) => el("button", {
    type: "button", "aria-pressed": String(state.source === s), text: s, onclick: () => { state.source = s; render(); },
  })));
}
// ---- config: the pipelines (YAML files), then factory.toml's settings ----
// YAML shown with light colouring: keys, comments, strings, outcomes. Each line is built from text nodes.
function yamlLine(line) {
  const out = [];
  const comment = line.match(/^(\s*)(#.*)$/);
  if (comment) return [comment[1], el("span", { class: "y-c", text: comment[2] })];
  const m = line.match(/^(\s*-?\s*)([A-Za-z0-9_.-]+|"[^"]*")(:)(\s*)(.*)$/);
  if (!m) return [yamlValue(line)];
  out.push(m[1], el("span", { class: /^[A-Z][A-Z0-9-]*$/.test(m[2]) ? "y-o" : "y-k", text: m[2] }), m[3], m[4]);
  if (m[5]) out.push(yamlValue(m[5]));
  return out;
}
function yamlValue(text) {
  const hash = text.search(/\s#/);
  const value = hash >= 0 ? text.slice(0, hash) : text;
  const rest = hash >= 0 ? text.slice(hash) : "";
  const cls = /^\s*("|')/.test(value) ? "y-s" : /^\s*(true|false|\d+)\s*$/.test(value) ? "y-n" : /^\s*[{[]/.test(value) ? "y-f" : "y-v";
  return el("span", {}, el("span", { class: cls, text: value }), rest ? el("span", { class: "y-c", text: rest }) : null);
}
function yamlView(text) {
  const lines = String(text || "").replace(/\n$/, "").split("\n");
  return el("pre", { class: "yaml", tabindex: 0 }, lines.map((line, i) => el("span", { class: "y-line" },
    el("span", { class: "y-no", "aria-hidden": "true", text: String(i + 1) }), el("span", { class: "y-text" }, yamlLine(line)))));
}
function configPipelines(project) {
  const info = project ? project.pipelines : state.pipelines;
  const list = project ? pipelinesOf(project) : staticPipelines();
  const holder = "cfgPipe";
  const name = pickedPipeline(holder, list);
  const chosen = list.find((p) => p.name === name) || null;
  const files = (info && info.files) || [];
  const problems = (info && info.problems) || [];
  const mine = jobs().filter((job) => !project || job.project === project.id);
  const tab = state.cfgTab || "dag";
  const head = el("div", { class: "head" },
    el("div", {}, el("h2", { text: "Pipes" }),
      el("p", { class: "sub" }, list.length + " pipe" + (list.length === 1 ? "" : "s") + " in ",
        el("code", { text: ((info && info.folder) || "factory/pipes") + "/" }),
        info && info.default ? el("span", {}, " · jobs run ", el("code", { text: info.default }), " unless told otherwise") : info && info.ask ? " · no [run].pipe: /mfactory run asks which one" : null)),
    problems.length ? el("span", { class: "pill st-failed" }, el("i", { class: "dot" }), problems.length + " problem" + (problems.length === 1 ? "" : "s")) : el("span", { class: "pill st-success" }, el("i", { class: "dot" }), "valid"));
  const rows = list.map((p) => {
    const errs = problems.filter((m) => p.file && m.startsWith(p.file));
    const count = mine.filter((job) => (job.pipeline || "default") === p.name).length;
    const on = chosen && chosen.name === p.name;
    return el("button", {
      type: "button", class: "pcard" + (on ? " on" : ""), "aria-pressed": String(!!on), "data-focus": "cfgpipe:" + p.name,
      onclick: () => { state[holder] = p.name; prefs.set("mfactory." + holder, p.name); render(); },
    },
      el("div", { class: "pc-top" }, el("span", { class: "mono name", text: p.name }),
        info && info.default === p.name ? el("span", { class: "chip", text: "default" }) : null,
        errs.length ? el("span", { class: "pill st-failed" }, el("i", { class: "dot" }), String(errs.length)) : null),
      el("div", { class: "dim", text: p.description || "No description" }),
      el("div", { class: "pc-meta mono dim" },
        el("span", { text: p.file === "built-in" ? "built-in" : p.file || "" }),
        p.extends ? el("span", { text: "extends " + p.extends }) : null,
        el("span", { text: (p.graph && p.graph.steps ? p.graph.steps.filter((s) => s.enabled !== false).length : p.steps || 0) + " steps" }),
        el("span", { text: count + " jobs" })));
  });
  const broken = files.filter((f) => f.errors && f.errors.length && !list.some((p) => p.file === f.path));
  broken.forEach((f) => rows.push(el("div", { class: "pcard bad" }, el("div", { class: "pc-top" }, el("span", { class: "mono name", text: f.path }),
    el("span", { class: "pill st-failed" }, el("i", { class: "dot" }), "not loaded")), el("div", { class: "dim", text: f.errors[0] }))));
  let detail = el("div", { class: "notice", text: "Pick a pipe." });
  if (chosen) {
    const fileErrors = problems.filter((m) => !chosen.file || m.startsWith(chosen.file) || m.includes('"' + chosen.name + '"'));
    const tabButton = (id, label) => el("button", {
      type: "button", role: "tab", "aria-selected": String(tab === id), "aria-pressed": tab === id ? "true" : "false", "data-focus": "cfgtab:" + id,
      onclick: () => { state.cfgTab = id; render(); },
    }, label);
    const body = tab === "yaml" ? (chosen.yaml ? yamlView(chosen.yaml) : el("p", { class: "dim", text: "No YAML for this pipe." }))
      : tab === "errors" ? (fileErrors.length ? el("ul", { class: "problems" }, fileErrors.map((m) => el("li", { class: "mono wrap", text: m }))) : el("p", { class: "good", text: "No problems. The file matches the schema and the graph is sound." }))
      : chosen.graph && Array.isArray(chosen.graph.steps)
        ? el("div", {}, dagView(chosen.graph, { counts: (s) => mine.filter((j) => (j.pipeline || "default") === chosen.name && j.step === s).length, zoomKey: "cfg:" + (project ? project.id : "") + ":" + chosen.name, label: "Pipe " + chosen.name }), dagLegend(false))
        : el("div", { class: "notice", text: "This pipe could not be drawn." });
    detail = el("div", { class: "pdetail" },
      el("div", { class: "head" }, el("div", {}, el("h3", { class: "mono", text: chosen.name }),
        el("div", { class: "dim mono", text: (chosen.file || "") + (chosen.graph ? " · start " + chosen.graph.start + " · escalate to " + chosen.graph.escalate_to : "") })),
        el("div", { class: "tabs", role: "tablist", "aria-label": "Pipe view" }, tabButton("dag", "DAG"), tabButton("yaml", "YAML"), tabButton("errors", "Problems" + (fileErrors.length ? " (" + fileErrors.length + ")" : "")))),
      body);
  }
  return el("div", { class: "panel pipes" }, head, info && info.legacy ? el("p", { class: "notice", text: "factory.toml still has [steps.*]: they override the default pipe. Run `mfactory migrate pipes` to move them to YAML." }) : null,
    el("div", { class: "pipes-body" }, el("div", { class: "pcards" }, rows), detail));
}
function configView() {
  const intro = [
    el("h1", { text: "Config" }),
    el("p", { class: "sub", text: "The pipes, YAML files in factory/pipes/ with a JSON Schema, and factory/factory.toml merged over the defaults" }),
  ];
  if (!state.workspace) {
    if (!state.config) return [el("h1", { text: "Config" }), el("p", { class: "sub", text: "Loading..." })];
    return intro.concat([configPipelines(null), el("h2", { class: "block-title", text: "Settings" }), sourceFilters(state.config), el("div", { class: "panel" }, configTable(state.config))]);
  }
  const projects = visibleProjects();
  if (!projects.length) return intro.concat([el("p", { class: "kempty", text: "no projects yet" })]);
  const allRows = projects.flatMap((project) => project.config || []);
  projects.forEach((project) => {
    if (projects.length > 1) intro.push(el("h2", { class: "block-title", text: project.name || project.id }));
    if (project.error) intro.push(el("p", { class: "error", text: String(project.error) }));
    intro.push(configPipelines(project));
  });
  intro.push(el("h2", { class: "block-title", text: "Settings" }), el("p", { class: "sub", text: "factory.toml merged over the defaults, and where each value comes from" }), sourceFilters(allRows));
  projects.forEach((project) => {
    if (projects.length > 1) intro.push(el("h3", { class: "block-title", text: project.name || project.id }));
    intro.push(el("div", { class: "panel" }, configTable(project.config)));
  });
  return intro;
}

// ---- shell ----
function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter((part) => part.length);
  const view = ["board", "pipeline", "jobs", "overview", "config"].includes(parts[0]) ? parts[0] : "board";
  if (view === "jobs" && state.workspace && parts.length >= 3) {
    return { view, project: decodeURIComponent(parts[1]), id: decodeURIComponent(parts[2]) };
  }
  return { view, project: null, id: view === "jobs" && parts[1] ? decodeURIComponent(parts[1]) : null };
}
function normalizeSelection() {
  if (!state.workspace || !state.projects.length) return;
  if (state.selected !== "all" && !state.projects.some((project) => project.id === state.selected)) {
    state.selected = "all";
    writeChoice("all");
    writeUrl("all");
  }
}
function renderFilters() {
  const box = document.getElementById("filters");
  if (!box) return;
  if (!state.workspace || !state.projects.length) {
    box.replaceChildren();
    return;
  }
  if (state.projects.length === 1) {
    const project = state.projects[0];
    box.replaceChildren(el("span", { class: "filters-one", text: "project: " + projectLabel(project) }));
    return;
  }
  const select = el("select", {
    class: "filters-select",
    onchange: (event) => chooseProject(event.target.value),
  }, [
    el("option", { value: "all", text: "all projects", selected: state.selected === "all" }),
    ...state.projects.map((project) => el("option", {
      value: project.id,
      text: projectLabel(project),
      selected: state.selected === project.id,
    })),
  ]);
  box.replaceChildren(el("label", { class: "filters-pick" }, "Project ", select));
}
function connectionLabel() {
  if (state.error) return "not connected";
  if (state.workspace && state.queueStale) return "queue stale";
  if (state.workspace) return state.projects.length ? "live" : "connecting";
  if (!state.index) return "connecting";
  return "live";
}
function paintLive() {
  paintAges();
  const live = document.getElementById("live");
  if (!live) return;
  const stale = !!(state.error || (state.workspace && state.queueStale));
  live.className = "live" + (stale ? " stale" : "");
  const label = connectionLabel();
  const status = document.getElementById("live-status");
  const clock = document.getElementById("clock");
  if (status) status.textContent = label;
  if (clock) clock.textContent = label === "live" ? new Date().toLocaleTimeString() : "";
  paintNowElapsed();
}
function paintNowElapsed() {
  if (route().view !== "board") return;
  document.querySelectorAll(".now-elapsed[data-started]").forEach((node) => {
    const started = time(node.getAttribute("data-started"));
    if (!started) {
      node.textContent = "";
      return;
    }
    node.textContent = durationLabel((Date.now() - started) / 1000);
  });
}
function workspaceTitle() {
  const projects = visibleProjects();
  if (state.selected !== "all" && projects[0]) return projects[0].name || projects[0].id;
  return "all";
}
function workspaceFoot() {
  const projects = visibleProjects();
  const versions = [...new Set(projects.map((project) => project.version).filter((version) => version))];
  const tools = [...new Set(projects.flatMap((project) => project.tools || []))];
  const bits = ["factory" + (versions.length ? " " + versions.join(", ") : "")];
  if (tools.length) bits.push(tools.join(", "));
  return bits.join(" · ");
}
function render() {
  normalizeSelection();
  renderFilters();
  const { view, id, project } = route();
  if (state.drawer && state.drawer.inline && !(view === "jobs" && id)) state.drawer = null;
  if (!(view === "jobs" && id)) state.jobNode = null;
  document.querySelectorAll("[data-view]").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  paintLive();
  const target = document.getElementById("view");
  const active = document.activeElement;
  const inside = !!(active && target.contains(active));
  const token = inside ? focusToken(active) : "";
  const keep = inside ? null : active;
  const scrollers = ".joblist, .file, .d-doc, .d-files, .d-runs, .d-timeline, .sh-out, .filelist, .yaml";
  const scroll = [...target.querySelectorAll(scrollers)].map((n) => n.scrollTop);
  // The board has a kanban per Jira board and one for the factory: each keeps its own scroll, by key.
  const keyed = new Map([...target.querySelectorAll("[data-scroll-key]")].map((n) => [n.getAttribute("data-scroll-key"), [n.scrollLeft, n.scrollTop]]));
  let content;
  const ready = state.workspace ? !state.error || state.projects.length : !!(state.index && state.graph);
  if (!ready) {
    content = [el("h1", { text: "factory" }), el("div", { class: "notice" }, state.error
      ? el("span", {}, "The dashboard's data could not be loaded: ", el("code", { text: state.error }), ". Start it with ", el("code", { text: "mfactory start dashboard" }), ".")
      : "Loading...")];
    document.getElementById("foot-left").textContent = FOOT_NOTE;
    document.getElementById("foot-right").textContent = "";
  } else if (state.workspace) {
    const totals = boardTotals(jobs());
    document.getElementById("foot-left").textContent = view === "board" || view === "overview"
      ? totals.running + " running · " + money(totals.cost) + " · " + FOOT_NOTE
      : FOOT_NOTE;
    document.getElementById("foot-right").textContent = workspaceFoot();
    document.title = "factory · " + workspaceTitle();
    content = view === "pipeline" ? pipeline() : view === "jobs" ? jobsView(id, project) : view === "overview" ? overview() : view === "config" ? configView() : board();
  } else {
    const totals = boardTotals(jobs());
    document.getElementById("foot-left").textContent = view === "board" || view === "overview"
      ? totals.running + " running · " + money(totals.cost) + " · " + FOOT_NOTE
      : FOOT_NOTE;
    document.getElementById("foot-right").textContent = `factory ${state.index.version} · ${state.index.tools.join(", ")}`;
    document.title = `factory · ${state.index.repo}`;
    content = view === "pipeline" ? pipeline() : view === "jobs" ? jobsView(id, project) : view === "overview" ? overview() : view === "config" ? configView() : board();
  }
  target.replaceChildren(...content);
  [...target.querySelectorAll(scrollers)].forEach((n, i) => { if (scroll[i]) n.scrollTop = scroll[i]; });
  target.querySelectorAll("[data-scroll-key]").forEach((n) => {
    const kept = keyed.get(n.getAttribute("data-scroll-key"));
    if (kept) { n.scrollLeft = kept[0]; n.scrollTop = kept[1]; }
  });
  if (state.drawer && state.drawer.focus) {
    const close = target.querySelector("[data-close]");
    if (close) close.focus();
    state.drawer.focus = false;
  } else if (token) {
    restoreFocus(target, token);
  } else if (keep && keep !== document.body) keep.focus({ preventScroll: true });
  paintNowElapsed();
}
async function load(path) {
  const res = await fetch(path, { cache: "no-store" });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.status);
  return data;
}
function activityView() {
  const view = route().view;
  return view === "board" || (view === "jobs" && !!(state.drawer && state.drawer.inline));
}
function openJob() {
  if (!state.drawer || !activityView()) return null;
  return jobs().find((job) => sameDrawer(job)) || null;
}
function refetchDrawer() {
  const open = openJob();
  if (open && state.drawer.path) fetchDrawer(open, state.drawer.path);
}
async function refreshNow() {
  const seq = (state.nowSeq = (state.nowSeq || 0) + 1);
  let list = [];
  try {
    list = jobs();
  } catch (error) {
    return;
  }
  const entries = await Promise.all(list.map(async (job) => {
    try {
      const res = await fetch(activityUrl(job), { cache: "no-store" });
      if (!res.ok) return [nowKey(job), null];
      return [nowKey(job), await res.json()];
    } catch (error) {
      return [nowKey(job), null];
    }
  }));
  if (seq !== state.nowSeq) return;
  const next = {};
  const seen = {};
  entries.forEach((entry) => {
    next[entry[0]] = entry[1];
    seen[entry[0]] = true;
  });
  const before = nowPanelKey();
  state.now = next;
  state.nowSeen = seen;
  if (route().view !== "board" || before === nowPanelKey()) return;
  render();
}
function focusToken(node) {
  const marked = node && node.closest ? node.closest("[data-focus]") : null;
  return marked ? marked.getAttribute("data-focus") || "" : "";
}
function restoreFocus(root, token) {
  const nodes = root.querySelectorAll("[data-focus]");
  for (const node of nodes) {
    if (node.getAttribute("data-focus") === token) {
      node.focus({ preventScroll: true });
      return;
    }
  }
}
function refreshActivity() {
  if (!state.drawer || state.drawer.tab !== "activity" || !activityView()) return;
  const open = openJob();
  if (open) fetchActivity(open);
}
async function fetchActivity(job) {
  const drawer = state.drawer;
  if (!drawer) return;
  const seq = (drawer.activitySeq = (drawer.activitySeq || 0) + 1);
  let body;
  try {
    const res = await fetch(activityUrl(job), { cache: "no-store" });
    body = res.ok ? await res.json() : { runs: [], error: "Could not load activity (" + res.status + ")." };
  } catch (error) {
    body = { runs: [], error: "Could not load activity." };
  }
  if (state.drawer !== drawer || drawer.activitySeq !== seq || drawer.tab !== "activity") return;
  const runs = Array.isArray(body.runs) ? body.runs : [];
  if (!drawer.runPicked) {
    const running = runs.findIndex((run) => run.status === "running");
    drawer.run = running >= 0 ? running : Math.max(0, runs.length - 1);
  } else if (drawer.run >= runs.length) {
    drawer.run = Math.max(0, runs.length - 1);
  }
  drawer.activity = body;
  render();
}
let probed = false;
let queueTimer = null;
let feedTimer = null;
async function refreshQueue() {
  if (!state.workspace) return;
  try {
    const body = await load("/queue.json");
    const rows = body.rows || [];
    const errors = body.errors || [];
    const changed = JSON.stringify([rows, errors]) !== JSON.stringify([state.queueRows, state.queueErrors]);
    state.queueRows = rows;
    state.queueErrors = errors;
    state.queueStale = false;
    if (changed) state.queueStamp += 1;
    if (changed) render();
    else paintLive();
  } catch (error) {
    state.queueStale = true;
    render();
  }
}
async function refreshWorkspace() {
  const body = await load("/projects.json");
  const next = body.projects || [];
  const plan = body.plan_usage || null;
  const changed = JSON.stringify([next, plan]) !== JSON.stringify([state.projects, state.planUsage]);
  state.projects = next;
  state.planUsage = plan;
  if (changed) state.jobsStamp += 1;
  const hadError = state.error;
  state.error = null;
  if (changed || hadError || route().view === "config") {
    render();
    refetchDrawer();
  } else {
    paintLive();
  }
  refreshActivity();
  refreshNow();
}
async function refreshSingle() {
  const [index, graph] = await Promise.all([load("jobs/index.json"), load("jobs/graph.json")]);
  const changed = JSON.stringify([index.jobs, graph, index.plan_usage]) !== JSON.stringify([state.index && state.index.jobs, state.graph, state.planUsage]);
  state.index = index; state.graph = graph; state.planUsage = index.plan_usage || null;
  const hadError = state.error; state.error = null;
  if (route().view === "config" || !state.config) state.config = await load("jobs/config.json");
  if (route().view === "config" || !state.pipelines) {
    try { state.pipelines = await load("jobs/pipelines.json"); } catch (error) { state.pipelines = state.pipelines || { pipelines: [] }; }
  }
  // Re-drawing only on change keeps selections and scroll positions while you read.
  if (changed || hadError || route().view === "config") {
    render();
    refetchDrawer();
  } else paintLive();
  refreshActivity();
  refreshNow();
}
async function tryProjects() {
  // false: this server has no workspace. null: try again next tick. object: the workspace.
  try {
    const res = await fetch("/projects.json", { cache: "no-store" });
    if (res.status === 404) return false;
    if (!res.ok) return null;
    const body = await res.json();
    if (!body || !Array.isArray(body.projects)) return false;
    return body;
  } catch (error) {
    return null;
  }
}
async function refresh() {
  try {
    if (!state.workspace && !probed) {
      const found = await tryProjects();
      if (found === null) return;
      if (found) {
        probed = true;
        state.workspace = true;
        state.projects = found.projects;
        state.planUsage = found.plan_usage || null;
        state.error = null;
        render();
        refetchDrawer();
        refreshQueue();
        refreshNow();
        refreshFeeds(false);
        if (!queueTimer) queueTimer = setInterval(refreshQueue, QUEUE_MS);
        if (!feedTimer) feedTimer = setInterval(() => refreshFeeds(false), FEED_TICK_MS);
        return;
      }
      probed = true;
    }
    if (state.workspace) {
      await refreshWorkspace();
      return;
    }
    await refreshSingle();
  } catch (e) {
    state.error = String(e.message || e);
    render();
  }
}
/* boot */
const search = document.getElementById("search");
if (/Mac|iPhone|iPad/.test(navigator.platform)) document.getElementById("hotkey").textContent = "⌘ K";
search.addEventListener("input", () => {
  state.query = search.value;
  if (route().view === "pipeline") location.hash = "#/jobs"; else render();
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); search.focus(); search.select(); }
  else if (e.key === "Escape" && state.jiraPicker) { e.preventDefault(); closePicker(); }
  else if (e.key === "Escape" && document.activeElement === search) { search.value = ""; state.query = ""; search.blur(); render(); }
  else if (e.key === "Escape" && state.drawer && route().view === "board") closeDrawer();
});
window.addEventListener("hashchange", () => {
  if (route().view === "config") refresh();
  if (state.jiraPicker && route().view !== "board") { state.jiraPicker = null; renderPicker(); }
  render();
  refreshFeeds(false);
});
render();
refresh();
setInterval(refresh, REFRESH_MS);
setInterval(paintLive, 1000);
