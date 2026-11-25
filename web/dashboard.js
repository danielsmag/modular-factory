"use strict";
// Everything shown here may come from a GitHub issue, so no markup is ever built
// from strings: nodes are created one by one and text goes in as text.
const SVG = "http://www.w3.org/2000/svg";
const REFRESH_MS = 5000;
const QUEUE_MS = 15000;
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
  split: readSplit(), now: {}, nowSeq: 0, nowSeen: {}, raw: false,
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
function link(url, text) {
  // Only real web addresses become links; anything else is shown as plain text.
  return /^https:\/\//.test(url || "") ? el("a", { class: "ext", href: url, target: "_blank", rel: "noopener noreferrer", text }) : el("span", { class: "mono", text });
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
  return [job.project, job.projectName, job.id, job.title, "#" + job.issue, job.step, job.author, job.pr ? "#" + job.pr : ""].some((v) => String(v || "").toLowerCase().includes(q));
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
  copy.graph = project.graph || null;
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
  return [row.project, row.projectName, row.title, row.note, "#" + row.issue, row.action, row.job, row.step, row.author, ...labels].some((value) => String(value || "").toLowerCase().includes(q));
}
function queueCards(rows, jobList) {
  const ids = new Set(jobList.map((job) => job.id));
  const issues = new Set(jobList.map((job) => String(job.issue)));
  const seen = new Set();
  const out = [];
  for (const row of rows) {
    if (row.action !== "new" && row.action !== "hold") continue;
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
function queueFor(projects) {
  const rows = [];
  projects.forEach((project) => {
    const mine = state.queueRows.filter((row) => row.project === project.id);
    queueCards(mine, project.jobs || []).forEach((row) => {
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
  return cols;
}
function columnWho(col) {
  if (col.id === "queue") return "not a job yet";
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
  if (who === "author") return "the author (@" + job.author + ")";
  if (who === "operator") return job.waiting.operator ? "the operator (@" + job.waiting.operator + ")" : "the operator";
  if (Array.isArray(who)) {
    const names = who.map((login) => "@" + login);
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
  if (Array.isArray(who)) return who.length === 1 ? "asked @" + who[0] : "asked " + who.length + " people";
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
      link(job.issue_url, "issue #" + (job.issue == null ? "" : job.issue)),
      job.pr ? link(job.pr_url, "PR #" + job.pr) : null),
    rounds.map((row) => roundLine(job, row)),
    el("div", { class: "meta" },
      job.intake && job.intake.risk ? el("span", { class: riskClass(job.intake.risk), text: job.intake.risk }) : null,
      el("span", { text: job.cost_usd !== null && job.cost_usd !== undefined ? money(job.cost_usd) : "—" }),
      elapsedNode(job)),
    hint ? el("p", { class: "hint", text: hint }) : null);
}
const FILE_ORDER = ["issue.md", "intake.md", "spec.md", "build.md", "decision.md", "verify.md", "rejection.md"];
const REVIEW_ORDER = ["code", "security", "ux", "ui"];
function fileRank(path) {
  const exact = FILE_ORDER.indexOf(path);
  if (exact >= 0) return [0, exact, 0, path];
  const feedback = String(path).match(/^feedback-(\d+)\.md$/);
  if (feedback) return [1, Number(feedback[1]), 0, path];
  const round = String(path).match(/^round-(\d+)\/(.+)\.(md|log)$/);
  if (round) {
    const stem = round[2];
    let kind = 50;
    if (stem === "checks") kind = round[3] === "log" ? 1 : 0;
    else if (stem.startsWith("review-")) {
      const at = REVIEW_ORDER.indexOf(stem.slice(7));
      kind = 10 + (at >= 0 ? at : 9);
    } else if (stem.startsWith("debug")) kind = 20;
    return [2, Number(round[1]), kind, path];
  }
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
  if (!round) return path;
  const stem = round[2].replace(/\.md$/, "").replace(/\.log$/, "");
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
          link(job.issue_url, "issue #" + (job.issue == null ? "" : job.issue)),
          job.pr ? link(job.pr_url, "PR #" + job.pr) : null),
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
  const fallback = "Could not load issue #" + drawer.issue + ".";
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
  const label = (row.title || row.note || "issue #" + (row.issue || "")) + (state.workspace && state.selected === "all" && row.projectName ? ", " + row.projectName : "");
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
      el("span", { class: "num", text: who + (row.action || "new") }),
      el("span", { class: "flag", text: "#" + (row.issue || "") })),
    el("p", { class: "feature", text: row.title || row.note || ("issue #" + (row.issue || "")) }),
    el("div", { class: "links" }, link(row.issue_url, "issue #" + (row.issue || ""))),
    opened || row.author || (row.labels || []).length ? el("div", { class: "meta" },
      opened ? el("span", { text: opened }) : null,
      opened ? ageNode(row.created_at, "ago") : null,
      row.author ? el("span", { text: "@" + row.author }) : null,
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
        el("div", { class: "step-meta", text: [comment.author ? "@" + comment.author : "", day(comment.created_at)].filter(Boolean).join(" · ") }),
        el("pre", { class: "file", text: comment.body || "" })))
      : el("p", { class: "kempty", text: "No comments." }));
}
function issueDrawer() {
  const drawer = state.drawer;
  const row = state.queueRows.find((item) => item.project === drawer.project && String(item.issue) === drawer.issue) || drawer.row;
  const data = drawer.data || {};
  const project = state.projects.find((item) => item.id === drawer.project);
  const close = el("button", { type: "button", class: "close", "data-close": "true", "data-focus": "drawer-close", text: "close", onclick: closeDrawer });
  const issueLink = link(data.url || row.issue_url, "issue #" + drawer.issue);
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
        el("div", { class: "num", text: (project ? projectLabel(project) : drawer.project) + " · issue #" + drawer.issue + (row.action ? " · " + row.action : "") }),
        el("h3", { id: "d-title", text: data.title || row.title || row.note || "issue #" + drawer.issue }),
        el("div", { class: "links" }, issueLink),
        el("div", { class: "facts" },
          author ? el("span", { text: "@" + author }) : null,
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
  const nodes = [
    el("div", { class: "head" },
      el("div", {}, el("h1", { text: "Board" }), el("p", { class: "sub", text: boardSub() })),
      el("span", { class: "src", text: shown.length + " of " + all.length })),
    ...problemLines(),
    nowPanel(shown),
    cols.length ? el("div", { class: "kanban" }, cols.map((col) => column(col, shown))) : el("p", { class: "kempty", text: state.workspace && !state.projects.length ? "no projects yet" : "no jobs" }),
  ];
  const panel = drawer();
  if (panel) nodes.push(panel);
  return nodes;
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
  if (!list.length) return el("div", { class: "empty", text: state.query ? "No job matches the search." : "No jobs yet. Label an issue and run /mfactory run." });
  const body = el("tbody", {}, list.map((job) => el("tr", {
    class: "link", tabindex: 0,
    onclick: () => { location.hash = jobHref(job); },
    onkeydown: (e) => { if (e.key === "Enter") location.hash = jobHref(job); },
  },
    el("td", { class: "mono", text: state.workspace && state.selected === "all" && job.projectName ? job.projectName + " · " + job.id : job.id }),
    el("td", { class: "wrap", text: job.title || "" }),
    el("td", {}, chip(job)),
    el("td", { class: "mono dim", text: job.round || "-" }),
    el("td", { class: "mono dim", text: job.pr ? "#" + job.pr : "-" }),
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
      el("div", { class: "panel" },
        el("h2", { text: split.priced ? "Cost by step" : "Time by step" }),
        el("p", { class: "sub", text: split.priced ? "API-equivalent, all jobs" : "Agents and scripts, all jobs" }),
        split.rows.length ? el("div", { class: "bars" }, split.rows.map(([step, v]) => el("div", { class: "bar" },
          el("div", { class: "row" }, el("span", { class: "name", text: step }), el("span", { class: "num", text: split.priced ? money(v) : span(v) })),
          el("div", { class: "track" }, el("div", { class: "fill", style: "width:" + Math.max(2, (100 * v) / split.rows[0][1]).toFixed(1) + "%" })),
          el("div", { class: "pct", text: ((100 * v) / split.sum).toFixed(1) + "%" }))))
          : el("div", { class: "empty", text: "Nothing has run yet." }))),
    el("div", { class: "panel" }, el("div", { class: "head" }, el("h2", { text: "Jobs" }), el("span", { class: "src", text: shown.length + " of " + all.length })), jobTable(shown)),
  ];
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
function lanes(groups) {
  // Shortest runs sit closest to the steps; runs that do not overlap share a lane.
  const taken = [];
  for (const group of groups.slice().sort((x, y) => (x.hi - x.lo) - (y.hi - y.lo))) {
    let lane = 0;
    while ((taken[lane] || []).some((g) => group.lo < g.hi + 8 && g.lo < group.hi + 8)) lane++;
    (taken[lane] = taken[lane] || []).push(group);
    group.lane = lane;
  }
  return taken.length;
}
function pipelineGraph(graph, count) {
  const NW = 200, LABEL = 152, LANE = 12, PORT = 16, GAP = 14, PAD = 12, R = 5;
  const names = order(graph), index = new Map(names.map((n, i) => [n, i]));
  const nodes = new Map();
  let y = PAD;
  for (const name of names) {
    const step = (graph.steps || []).find((item) => item.name === name) || null;
    const edges = step ? step.edges.filter((e) => index.has(e.to)) : [];
    const fwd = edges.filter((e) => index.get(e.to) > index.get(name)), back = edges.filter((e) => index.get(e.to) <= index.get(name));
    const h = Math.max(42, 26 + PORT * (Math.max(fwd.length, back.length, 1) - 1) + 12);
    nodes.set(name, { name, step, y, h, fwd, back });
    y += h + GAP;
  }
  const H = y - GAP + PAD;
  const runs = [];
  for (const node of nodes.values()) {
    for (const [side, list] of [["fwd", node.fwd], ["back", node.back]]) {
      list.forEach((edge, i) => {
        const target = nodes.get(edge.to);
        runs.push({ side, from: node.name, to: edge.to, edge, y1: node.y + 26 + PORT * i, y2: target.y + 9,
          off: node.step.enabled === false && edge.outcome !== node.step.skip });
      });
    }
  }
  const groups = { fwd: new Map(), back: new Map() };
  for (const run of runs) {
    const group = groups[run.side].get(run.to) || { lo: Infinity, hi: -Infinity };
    group.lo = Math.min(group.lo, run.y1, run.y2);
    group.hi = Math.max(group.hi, run.y1, run.y2);
    groups[run.side].set(run.to, group);
  }
  const leftLanes = lanes([...groups.back.values()]), rightLanes = lanes([...groups.fwd.values()]);
  const xl = PAD + Math.max(0, leftLanes - 1) * LANE + (leftLanes ? LABEL : 0), xr = xl + NW;
  const W = xr + (rightLanes ? LABEL : 0) + Math.max(0, rightLanes - 1) * LANE + PAD;

  const root = svg("svg", { class: "graph", viewBox: `0 0 ${W} ${H}`, style: `width:${W}px`, role: "img",
    "aria-label": "The pipeline's steps and the edges between them. The Steps table lists the same." });
  const marker = (id, colour) => svg("marker", { id, viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 6, markerHeight: 6, orient: "auto" }, svg("path", { d: "M0,0 L8,4 L0,8 z", fill: colour }));
  root.append(svg("defs", {}, marker("arrow", "#8a8a8a"), marker("arrow-back", "#c9836f"), marker("arrow-off", "#3a3a3a")));
  const wires = svg("g", {}), labels = svg("g", {});
  for (const run of runs.sort((a, b) => Number(b.off) - Number(a.off))) {
    const right = run.side === "fwd", dir = right ? 1 : -1, down = run.y2 > run.y1 ? 1 : -1;
    const x0 = right ? xr : xl, lane = x0 + dir * (LABEL + groups[run.side].get(run.to).lane * LANE);
    const d = `M${x0},${run.y1} H${lane - dir * R} Q${lane},${run.y1} ${lane},${run.y1 + down * R} V${run.y2 - down * R} Q${lane},${run.y2} ${lane - dir * R},${run.y2} H${x0 + dir * 2}`;
    const kind = run.off ? " off" : right ? "" : " back";
    wires.append(svg("path", { class: "edge" + kind, d, "marker-end": `url(#arrow${run.off ? "-off" : right ? "" : "-back"})`, "data-from": run.from, "data-to": run.to }));
    const text = run.edge.outcome + (run.edge.max !== null && run.edge.max !== undefined ? ` · max ${run.edge.max}` : "");
    labels.append(svg("circle", { class: "port" + kind, cx: x0, cy: run.y1, r: 2.5, "data-from": run.from, "data-to": run.to }),
      svg("text", { class: "elabel" + kind, x: x0 + dir * 8, y: run.y1 - 6, "text-anchor": right ? "start" : "end", text, "data-from": run.from, "data-to": run.to }));
  }
  root.append(wires);
  for (const node of nodes.values()) {
    const { name, step, h } = node, n = count(name), end = !step;
    const waiting = step && step.type === "human" && n > 0;
    const kind = end ? "end" : step.type + (step.who ? " · " + step.who : "") + (step.enabled === false ? " · off" : "");
    const group = svg("g", { class: "node" + (end ? " end" : "") + (step && step.enabled === false ? " off" : "") + (waiting ? " wait" : "") });
    group.append(svg("rect", { x: xl, y: node.y, width: NW, height: h, rx: 3 }),
      svg("text", { class: "name", x: xl + 11, y: node.y + 18, text: name.length > 20 ? name.slice(0, 19) + "…" : name }),
      svg("text", { class: "kind", x: xl + 11, y: node.y + 32, text: kind }));
    if (n) group.append(svg("g", { class: "count" }, svg("rect", { x: xr - 40, y: node.y + 8, width: 28, height: 20, rx: 10 }), svg("text", { x: xr - 26, y: node.y + 22, "text-anchor": "middle", text: n })));
    group.append(svg("title", { text: `${name}: ${n} job${n === 1 ? "" : "s"}` }));
    const focus = (on) => {
      root.classList.toggle("focus", on);
      for (const part of root.querySelectorAll("[data-from]")) part.classList.toggle("hot", on && (part.dataset.from === name || part.dataset.to === name));
    };
    group.addEventListener("mouseenter", () => focus(true));
    group.addEventListener("mouseleave", () => focus(false));
    root.append(group);
  }
  root.append(labels);
  return root;
}
function pipelineDiagram(graph, jobList) {
  if (!graph || !Array.isArray(graph.steps) || !Array.isArray(graph.end_states)) {
    return [el("div", { class: "notice", text: "No pipeline for this project yet." })];
  }
  let drawn;
  try {
    const count = (name) => jobList.filter((j) => j.step === name).length;
    const off = (step) => step.enabled === false;
    const stepRows = graph.steps.map((step) => el("tr", {},
      el("td", { class: "mono", text: step.name }),
      el("td", {}, el("span", { class: "chip" + (off(step) ? " off" : ""), text: step.type + (off(step) ? " · off" : "") }),
        step.who ? el("div", { class: "dim step-who", text: step.who }) : null),
      el("td", { class: "mono wrap" }, (step.edges || []).map((e) => el("div", { class: off(step) && e.outcome !== step.skip ? "dim" : "" },
        e.outcome, el("span", { class: "dim", text: " → " }), e.to, e.max !== null && e.max !== undefined ? el("span", { class: "dim", text: `  max ${e.max}` }) : null))),
      el("td", { class: "mono dim", text: count(step.name) || "-" })));
    drawn = [el("div", { class: "pipe" },
      el("div", { class: "panel" }, el("div", { class: "scroll" }, pipelineGraph(graph, count)),
        el("div", { class: "legend" }, el("span", {}, el("i"), "on to a later step"), el("span", {}, el("i", { class: "back" }), "back to an earlier step"),
          el("span", {}, el("i", { class: "box" }), "waiting for a person"), el("span", {}, el("i", { class: "box off" }), "step is off"))),
      el("div", { class: "panel" }, el("h2", { text: "Steps" }), el("p", { class: "sub", text: "As set in factory.toml over the defaults" }),
        el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, ["Step", "Type", "Outcomes", "Jobs"].map((t) => el("th", { scope: "col", text: t })))), el("tbody", {}, stepRows)))))];
  } catch (error) {
    drawn = [el("div", { class: "notice", text: "This pipeline could not be drawn." })];
  }
  return drawn;
}
function pipelineIntro(graph) {
  return el("p", { class: "sub", text: `Starts at ${graph.start}. When an edge's max is used up, the job goes to ${graph.escalate_to}.` });
}
function pipeline() {
  if (!state.workspace) {
    return [el("h1", { text: "Pipeline" }), pipelineIntro(state.graph), ...pipelineDiagram(state.graph, jobs())];
  }
  const projects = visibleProjects();
  if (projects.length <= 1) {
    const project = projects[0];
    const nodes = [el("h1", { text: "Pipeline" })];
    if (!project) return nodes.concat([el("p", { class: "kempty", text: "no projects yet" })]);
    if (project.error) nodes.push(el("p", { class: "error", text: String(project.error) }));
    if (project.graph) nodes.push(pipelineIntro(project.graph));
    nodes.push(...pipelineDiagram(project.graph, jobs()));
    return nodes;
  }
  const nodes = [
    el("h1", { text: "Pipeline" }),
    el("p", { class: "sub", text: "One pipeline per project. Each factory.toml can differ." }),
  ];
  projects.forEach((project) => {
    const mine = jobs().filter((job) => job.project === project.id);
    nodes.push(el("h2", { class: "block-title", text: project.name || project.id }));
    if (project.path) nodes.push(el("p", { class: "sub", text: project.path }));
    if (project.error) nodes.push(el("p", { class: "error", text: String(project.error) }));
    if (project.graph) nodes.push(pipelineIntro(project.graph));
    nodes.push(...pipelineDiagram(project.graph, mine));
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
function jobDetail(job) {
  const step = stepOf(job.step, job);
  const parts = [];
  parts.push(el("div", { class: "panel" },
    el("div", { class: "head" }, el("div", { style: "min-width:0" }, el("h2", { class: "job-title", text: job.title }), el("div", { class: "mono dim", style: "margin-top:3px", text: (state.workspace && state.selected === "all" && job.projectName ? job.projectName + " · " : "") + job.id })), chip(job)),
    el("div", { class: "facts" },
      el("span", {}, "Issue ", link(job.issue_url, "#" + job.issue)),
      job.pr ? el("span", {}, "PR ", link(job.pr_url, "#" + job.pr)) : null,
      el("span", {}, "By ", el("b", { text: "@" + job.author })),
      job.intake ? el("span", {}, "Risk ", el("b", { text: job.intake.risk })) : null,
      job.intake ? el("span", {}, "Surfaces ", el("b", { text: (job.intake.surfaces || []).join(", ") || "none" })) : null,
      el("span", {}, "Round ", el("b", { text: `${job.round || 0}` + (job.attempt > 1 ? `, attempt ${job.attempt}` : "") })),
      job.cost_usd !== null && job.cost_usd !== undefined ? el("span", {}, "Cost ", el("b", { text: money(job.cost_usd) })) : null,
      el("span", {}, "Started ", el("b", {}, ageNode(job.created_at, "ago"))))));

  if (job.waiting) {
    parts.push(el("div", { class: "ask" },
      el("h2", {}, "Waiting for " + waitingAudience(job) + " since ", ageNode(job.waiting && job.waiting.since, "ago")),
      el("p", { text: job.reason || (job.waiting.replies === "tool"
        ? `Step ${job.step} needs a reply in your coding tool. A comment on the ${job.waiting.asked_on === "pr" ? "pull request" : "issue"} does not count.`
        : `Step ${job.step} needs a reply. Type one of these in your coding tool, or comment it on the ${job.waiting.asked_on === "pr" ? "pull request" : "issue"} without the number.`) }),
      commands(job).map((cmd) => el("div", { class: "cmd" }, el("code", { text: cmd }),
        el("button", { type: "button", text: "Copy", onclick: async (e) => {
          const literal = cmd.replace(/ <[^>]+>$/, " ");
          try { await navigator.clipboard.writeText(literal); e.target.textContent = "Copied"; } catch (_) { e.target.textContent = "Select it"; }
          setTimeout(() => { e.target.textContent = "Copy"; }, 1500);
        } })))));
  } else if (job.reason && isEnd(job.step, job) && job.step !== "done") {
    parts.push(el("div", { class: "notice", style: "white-space:pre-wrap;overflow-wrap:anywhere", text: job.reason }));
  }

  const items = (job.history || []).map((h) => {
    const bad = /^(FAIL|FAILED|REJECT|CHANGES|BLOCKED|ESCALATE|UNKNOWN|NEEDS-INFO)$/.test(h.outcome);
    return el("li", { class: bad ? "ko" : "ok" }, el("span", { class: "mark" }), el("span", { class: "mono", text: h.step }),
      el("span", { class: "what" }, el("span", { class: "mono", text: h.outcome + " → " + h.to }),
        h.skipped && h.skipped.length ? el("span", { class: "dim", text: "  skipped " + h.skipped.join(", ") }) : null,
        h.reason ? el("div", { class: "dim history-reason", text: h.reason.split("\n")[0].slice(0, 200) }) : null),
      el("span", { class: "when" }, span((time(h.at) - time(h.entered_at)) / 1000) + " · ", ageNode(h.at, "ago")));
  });
  if (!isEnd(job.step, job)) {
    items.push(el("li", { class: "now" }, el("span", { class: "mark" }), el("span", { class: "mono", text: job.step }),
      el("span", { class: "what dim", text: job.waiting ? "waiting for a person" : step ? `${step.type} step, in progress` : "this step is no longer in factory.toml" }),
      el("span", { class: "when" }, ageNode(job.entered_at, "since"))));
  }
  parts.push(el("div", { class: "panel" }, el("h2", { text: "Steps" }), el("p", { class: "sub", text: "In the order the job took them" }), el("ol", { class: "steps" }, items)));

  const files = job.files || [];
  const open = sameFile(job) ? state.file : null;
  parts.push(el("div", { class: "panel" }, el("h2", { text: "Files" }), el("p", { class: "sub", text: "What each step wrote, with its first line" }),
    files.length ? el("div", { class: "files", style: splitStyle() },
      el("div", { class: "filelist" }, files.map((f) => el("button", { type: "button", "aria-pressed": String(!!open && open.path === f.path), onclick: () => showFile(job, f.path) },
        el("span", { class: "path", text: f.path }), el("span", { class: "verdict", text: f.first_line || "" })))),
      grip(),
      fileView({ path: open ? open.path : null, text: open ? open.text : "Pick a file to read it." }))
      : el("div", { class: "empty", text: "No files yet." })));
  return el("div", { class: "detail" }, parts);
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
function jobsView(id, projectId) {
  const all = jobs(), shown = all.filter(matches);
  const current = all.find((job) => sameJob(job, projectId, id)) || shown[0] || null;
  if (!all.length) return [el("h1", { text: "Jobs" }), el("p", { class: "sub", text: "One job per labeled issue" }), el("div", { class: "notice" }, "No jobs yet. Label an issue ", el("code", { text: queueLabel() }), " and run ", el("code", { text: "/mfactory run" }), " in your coding tool.")];
  return [
    el("h1", { text: "Jobs" }),
    el("p", { class: "sub", text: `${shown.length} of ${all.length}` + (state.query ? " match the search" : "") }),
    el("div", { class: "jobview" },
      el("nav", { class: "panel joblist", "aria-label": "Jobs" }, shown.length ? shown.map((job) => el("a", { href: jobHref(job), "aria-current": !!current && sameJob(job, current.project || null, current.id) ? "true" : null },
        el("div", { class: "id", text: state.workspace && state.selected === "all" ? (job.projectName || job.project) + " · " + job.id : job.id }),
        el("div", { class: "meta" }, el("span", {}, "#" + job.issue + " · ", ageNode(job.updated_at, "ago")), chip(job)))) : el("div", { class: "empty", text: "No job matches." })),
      current ? jobDetail(current) : el("div", { class: "notice", text: "Pick a job." })),
  ];
}

// ---- config ----
function configRows(rows) {
  const q = state.query.trim().toLowerCase();
  return rows.filter((r) => (state.source === "all" || r.source === state.source) && (!q || String(r.key).toLowerCase().includes(q) || JSON.stringify(r.value).toLowerCase().includes(q)));
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
  const sources = ["all", ...new Set((rows || []).map((r) => r.source).filter((source) => source))];
  return el("div", { class: "filters", role: "group", "aria-label": "Source" }, sources.map((s) => el("button", {
    type: "button", "aria-pressed": String(state.source === s), text: s, onclick: () => { state.source = s; render(); },
  })));
}
function configView() {
  const intro = [
    el("h1", { text: "Config" }),
    el("p", { class: "sub", text: "factory.toml merged over the defaults, and where each value comes from" }),
  ];
  if (!state.workspace) {
    if (!state.config) return [el("h1", { text: "Config" }), el("p", { class: "sub", text: "Loading..." })];
    return intro.concat([sourceFilters(state.config), el("div", { class: "panel" }, configTable(state.config))]);
  }
  const projects = visibleProjects();
  if (!projects.length) return intro.concat([el("p", { class: "kempty", text: "no projects yet" })]);
  const allRows = projects.flatMap((project) => project.config || []);
  intro.push(sourceFilters(allRows));
  projects.forEach((project) => {
    if (projects.length > 1) intro.push(el("h2", { class: "block-title", text: project.name || project.id }));
    if (project.error) intro.push(el("p", { class: "error", text: String(project.error) }));
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
  }
}
function filterButton(id, text) {
  return el("button", {
    type: "button", text, "aria-pressed": state.selected === id ? "true" : "false",
    onclick: () => { state.selected = id; writeChoice(id); render(); refreshNow(); },
  });
}
function renderFilters() {
  const box = document.getElementById("filters");
  if (!box) return;
  if (!state.workspace || state.projects.length < 2) {
    box.replaceChildren();
    return;
  }
  const buttons = [filterButton("all", "all")];
  state.projects.forEach((project) => buttons.push(filterButton(project.id, projectLabel(project))));
  box.replaceChildren(...buttons);
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
  document.querySelectorAll("[data-view]").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  paintLive();
  const target = document.getElementById("view");
  const active = document.activeElement;
  const inside = !!(active && target.contains(active));
  const token = inside ? focusToken(active) : "";
  const keep = inside ? null : active;
  const scrollers = ".joblist, .file, .d-doc, .d-files, .d-runs, .d-timeline, .sh-out, .filelist";
  const scroll = [...target.querySelectorAll(scrollers)].map((n) => n.scrollTop);
  const kanban = target.querySelector(".kanban");
  const kanbanLeft = kanban ? kanban.scrollLeft : 0;
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
  const nextKanban = target.querySelector(".kanban");
  if (nextKanban && kanbanLeft) nextKanban.scrollLeft = kanbanLeft;
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
function openJob() {
  if (!state.drawer || route().view !== "board") return null;
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
  if (!state.drawer || state.drawer.tab !== "activity" || route().view !== "board") return;
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
  const changed = JSON.stringify(next) !== JSON.stringify(state.projects);
  state.projects = next;
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
  const changed = JSON.stringify([index.jobs, graph]) !== JSON.stringify([state.index && state.index.jobs, state.graph]);
  state.index = index; state.graph = graph;
  const hadError = state.error; state.error = null;
  if (route().view === "config" || !state.config) state.config = await load("jobs/config.json");
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
        state.error = null;
        render();
        refetchDrawer();
        refreshQueue();
        refreshNow();
        if (!queueTimer) queueTimer = setInterval(refreshQueue, QUEUE_MS);
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
  else if (e.key === "Escape" && document.activeElement === search) { search.value = ""; state.query = ""; search.blur(); render(); }
  else if (e.key === "Escape" && state.drawer && route().view === "board") closeDrawer();
});
window.addEventListener("hashchange", () => { if (route().view === "config") refresh(); render(); });
render();
refresh();
setInterval(refresh, REFRESH_MS);
setInterval(paintLive, 1000);
